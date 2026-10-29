"""Authenticated, default-deny quality HTTP contracts on synthetic /tmp DBs."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import numpy as np
import pytest

from prediction_api import create_app
from quality_review_store import QualityReviewStore
from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_store import ReceptionStore


TOKEN = "synthetic-quality-shared-token-never-persist-1234567890"
STAFF = {"X-Staff-Token": TOKEN}
ACTOR = {"principal_kind": "shared_staff_token", "principal_id": "shared-staff"}
ORIGIN = "http://127.0.0.1"
RESULT_FIELDS = {"operation_id", "reception_id", "action", "outcome", "response_status",
                 "recorded_at", "resulting_revision", "quality_result", "error"}


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 30, 18, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


def app_for(database, clock):
    return create_app(db_path=database, clock=clock, staff_token=TOKEN,
                      model_path=database.parent / "missing-synthetic-model.joblib")


@contextmanager
def environment(*, version=2, mode="revision_required", enabled=1):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, "quality-api.sqlite3", populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
            with lab.connection(database) as conn:
                conn.execute("UPDATE store_metadata SET staff_write_mode=?,seat_corrections_enabled=?",
                             (mode, enabled))
        clock = Clock()
        app = app_for(database, clock)
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50111)) as client:
            yield lab, database, clock, app, client


@pytest.fixture
def quality_api():
    with environment() as state:
        yield state


def register(client):
    response = client.post("/receptions", json={"adults": 2, "children": 0},
                           headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 201
    return response.json()


def setup_issue(client, app, clock):
    receipt = register(client)
    store = app.state.store
    current = store.get_staff_reception(receipt["id"])
    clock.value += timedelta(minutes=10)
    store.staff_transition(receipt["id"], "seat", operation_id=str(uuid4()),
                           expected_revision=current["revision"], write_epoch=current["write_epoch"], **ACTOR)
    target = store.get_events(receipt["id"])[-1]
    clock.value += timedelta(minutes=1)
    store.correct_seating(receipt["id"], operation_id=str(uuid4()), expected_revision=1,
                          write_epoch=current["write_epoch"], target_event_id=target["event_id"],
                          reason_code="accidental_seating", reason_detail="合成記録の誤操作訂正", **ACTOR)
    clock.value += timedelta(minutes=14)
    store.staff_transition(receipt["id"], "seat", operation_id=str(uuid4()), expected_revision=2,
                           write_epoch=current["write_epoch"], **ACTOR)
    return receipt


def context(client, receipt):
    response = client.get(f"/staff/receptions/{receipt['id']}/quality-context", headers=STAFF)
    assert response.status_code == 200
    return response.json()


def evidence():
    return dict(reference="synthetic/records-001", intake_verified=True,
                seating_verified=True, context_understood=True)


def body(client, receipt, *, kind="review", decision="hold", **overrides):
    current = context(client, receipt)
    result = dict(operation_id=str(uuid4()), expected_revision=current["revision"],
                  write_epoch=current["write_epoch"], expected_watermarks=current["watermarks"],
                  decision=decision, reason_code="synthetic_check", reason_detail="合成記録の確認待ち",
                  evidence=evidence() if decision in {"clear", "eligible"} else {})
    if kind == "review":
        issue = current["issues"][0]
        result.update(issue_id=issue["issue_id"],
                      expected_review_number=issue["latest_review"]["review_number"] if issue["latest_review"] else 0)
    else:
        result.update(expected_assessment_id=current["latest_assessment_id"])
    result.update(overrides)
    return result


def command(client, receipt, payload, *, kind="review", headers=STAFF):
    route = "quality-reviews" if kind == "review" else "quality-assessments"
    return client.post(f"/staff/receptions/{receipt['id']}/{route}", json=payload, headers=headers)


def table(lab, database, name):
    with lab.connection(database, readonly=True) as conn:
        cursor = conn.execute(f"SELECT * FROM {name}")
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor]


@pytest.mark.parametrize("kind", ["review", "assessment"])
@pytest.mark.parametrize("decision", ["hold", "exclude"])
def test_shared_staff_can_append_conservative_decision_and_inspect_exact_saved_result(quality_api, kind, decision):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    previous = app.state.store.get_staff_reception(receipt["id"])
    payload = body(client, receipt, kind=kind, decision=decision)
    response = command(client, receipt, payload, kind=kind)
    assert response.status_code == 200
    result = response.json()
    assert set(result) == RESULT_FIELDS
    assert result["action"] == ("quality_review" if kind == "review" else "quality_assessment")
    assert result["outcome"] == ("review_added" if kind == "review" else "assessment_added")
    assert result["quality_result"]["decision"] == decision
    assert result["quality_result"]["reason_detail"] == payload["reason_detail"]
    assert result["resulting_revision"] == previous["revision"]
    assert response.headers["cache-control"] == "no-store"
    lookup = client.get(f"/staff/operations/{payload['operation_id']}", headers=STAFF)
    assert lookup.status_code == 200 and lookup.json() == result
    assert app.state.store.get_staff_reception(receipt["id"]) == previous
    quality = client.get(f"/staff/receptions/{receipt['id']}/data-quality", headers=STAFF).json()
    assert quality["training_use"] == "blocked"
    current = context(client, receipt)
    if kind == "review":
        assert current["issues"][0]["latest_review"] == result["quality_result"]
    else:
        assert current["latest_assessment_id"] == result["quality_result"]["assessment_id"]
    operation = table(lab, database, "staff_operations")[-1]
    assert operation["principal_kind"] == "shared_staff_token"
    assert operation["principal_id"] == "shared-staff"
    assert TOKEN not in database.read_bytes().decode("utf-8", errors="ignore")


@pytest.mark.parametrize("kind,decision", [("review", "clear"), ("assessment", "eligible")])
def test_http_never_enables_release_with_shared_authentication_or_client_evidence(quality_api, kind, decision):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt, kind=kind, decision=decision)
    response = command(client, receipt, payload, kind=kind)
    assert response.status_code == 403
    result = response.json()
    assert set(result) == RESULT_FIELDS and result["outcome"] == "rejected"
    assert result["quality_result"] is None
    assert result["error"]["code"] == "quality_authorization_required"
    assert not table(lab, database, "data_quality_reviews")
    assert not table(lab, database, "data_quality_assessments")
    before = database.read_bytes()
    assert command(client, receipt, payload, kind=kind).json() == result
    assert database.read_bytes() == before
    lookup = client.get(f"/staff/operations/{payload['operation_id']}", headers=STAFF)
    assert lookup.status_code == 200 and lookup.json() == result


@pytest.mark.parametrize("endpoint", ["context", "review", "assessment", "result"])
@pytest.mark.parametrize("headers", [{}, {"X-Staff-Token": "incorrect-synthetic-token"}])
def test_all_quality_paths_and_result_lookup_require_authentication_without_writing(quality_api, endpoint, headers):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt)
    operation = command(client, receipt, payload).json()
    before = database.read_bytes()
    if endpoint == "context":
        response = client.get(f"/staff/receptions/{receipt['id']}/quality-context", headers=headers)
    elif endpoint == "result":
        response = client.get(f"/staff/operations/{operation['operation_id']}", headers=headers)
    else:
        request_body = body(client, receipt, kind=endpoint)
        response = command(client, receipt, request_body, kind=endpoint, headers=headers)
    assert response.status_code == 401
    assert database.read_bytes() == before
    assert "quality_result" not in response.json()


@pytest.mark.parametrize("kind", ["review", "assessment"])
def test_foreign_origin_cannot_create_quality_decisions(quality_api, kind):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt, kind=kind)
    before = database.read_bytes()
    response = command(client, receipt, payload, kind=kind,
                       headers=STAFF | {"Origin": "https://untrusted.invalid"})
    assert response.status_code == 403
    assert database.read_bytes() == before


@pytest.mark.parametrize("kind", ["review", "assessment"])
@pytest.mark.parametrize("field,value", [("principal_kind", "staff_account"),
                                         ("principal_id", "claimed-person-name"),
                                         ("authorization", {"authority_id": "forged"}),
                                         ("authority", "forged-verifier"),
                                         ("expected_revision", True), ("expected_revision", "3"),
                                         ("operation_id", "not-a-uuid"), ("write_epoch", None),
                                         ("reason_detail", " "), ("decision", "unknown")])
def test_strict_input_rejects_client_authority_claims_and_malformed_commands(quality_api, kind, field, value):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt, kind=kind)
    payload[field] = value
    before = database.read_bytes()
    response = command(client, receipt, payload, kind=kind)
    assert response.status_code == 422
    assert database.read_bytes() == before
    assert "claimed-person-name" not in response.text


@pytest.mark.parametrize("mutation", ["missing", "extra", "boolean", "string"])
def test_watermark_shape_and_integer_types_are_strict(quality_api, mutation):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt)
    if mutation == "missing":
        del payload["expected_watermarks"]["issue_id"]
    elif mutation == "extra":
        payload["expected_watermarks"]["unknown"] = 1
    elif mutation == "boolean":
        payload["expected_watermarks"]["review_id"] = False
    else:
        payload["expected_watermarks"]["event_id"] = "4"
    before = database.read_bytes()
    assert command(client, receipt, payload).status_code == 422
    assert database.read_bytes() == before


@pytest.mark.parametrize("kind", ["review", "assessment"])
def test_response_loss_retry_and_restarted_api_recover_exact_result_once(quality_api, kind):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt, kind=kind)
    original = command(client, receipt, payload, kind=kind).json()
    # A fresh application must validate the parent and immutable decision child.
    restarted = app_for(database, clock)
    with TestClient(restarted, base_url=ORIGIN, client=("127.0.0.1", 50222)) as recovered:
        before = database.read_bytes()
        for _ in range(2):
            assert command(recovered, receipt, payload, kind=kind).json() == original
            lookup = recovered.get(f"/staff/operations/{payload['operation_id']}", headers=STAFF)
            assert lookup.status_code == 200 and lookup.json() == original
            assert database.read_bytes() == before
    name = "data_quality_reviews" if kind == "review" else "data_quality_assessments"
    assert len(table(lab, database, name)) == 1


@pytest.mark.parametrize("kind", ["review", "assessment"])
def test_different_request_cannot_reuse_a_quality_operation_id(quality_api, kind):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt, kind=kind)
    original = command(client, receipt, payload, kind=kind).json()
    before = database.read_bytes()
    response = command(client, receipt, payload | {"reason_detail": "別の要求内容"}, kind=kind)
    assert response.status_code == 409
    assert database.read_bytes() == before
    assert client.get(f"/staff/operations/{payload['operation_id']}", headers=STAFF).json() == original


@pytest.mark.parametrize("kind", ["review", "assessment"])
def test_stale_quality_snapshot_is_archived_conflict_not_a_second_decision(quality_api, kind):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    stale = body(client, receipt, kind=kind)
    assert command(client, receipt, body(client, receipt), kind="review").status_code == 200
    response = command(client, receipt, stale, kind=kind)
    assert response.status_code == 409 and response.json()["outcome"] == "rejected"
    assert len(table(lab, database, "data_quality_reviews")) == 1
    assert not table(lab, database, "data_quality_assessments")
    assert client.get(f"/staff/operations/{stale['operation_id']}", headers=STAFF).json() == response.json()


@pytest.mark.parametrize("kind", ["review", "assessment"])
def test_database_failure_after_quality_child_returns_503_and_rolls_back_everything(quality_api, monkeypatch, kind):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt, kind=kind)
    name = "_save_review" if kind == "review" else "_save_assessment"
    original = getattr(QualityReviewStore, name)

    def interrupted(conn, row):
        original(conn, row)
        raise sqlite3.OperationalError("injected unavailable transaction")

    monkeypatch.setattr(QualityReviewStore, name, staticmethod(interrupted))
    before = database.read_bytes()
    response = command(client, receipt, payload, kind=kind)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert database.read_bytes() == before
    assert not table(lab, database, "data_quality_reviews")
    assert not table(lab, database, "data_quality_assessments")
    monkeypatch.undo()
    assert command(client, receipt, payload, kind=kind).status_code == 200
    assert ReceptionStore(database, clock=clock).get(receipt["id"])["actual_wait_minutes"] == 25


def test_quality_internal_data_is_not_added_to_public_customer_responses(quality_api):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt)
    assert command(client, receipt, payload).status_code == 200
    fresh = register(client)
    public = client.get("/public/queue")
    assert public.status_code == 200
    forbidden = {"revision", "write_epoch", "operation_id", "quality_result", "issues",
                 "principal_kind", "principal_id", "request_sha256", "expected_watermarks"}

    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert not keys(fresh) & forbidden
    assert not keys(public.json()) & forbidden
    html = client.get("/")
    assert html.status_code == 200 and TOKEN not in html.text
    assert "quality-reviews" not in html.text and "quality-assessments" not in html.text


@pytest.mark.parametrize("version,mode", [(1, "legacy"), (2, "legacy")])
def test_quality_commands_refuse_v1_and_legacy_without_migration_or_mode_change(version, mode):
    with environment(version=version, mode=mode, enabled=0) as (lab, database, clock, app, client):
        receipt = register(client)
        payload = dict(operation_id=str(uuid4()), expected_revision=0, write_epoch=str(uuid4()),
                       expected_watermarks=dict(event_id=1, issue_id=0, review_id=0),
                       expected_assessment_id=0, decision="hold", reason_code="pending",
                       reason_detail="未確認の合成記録", evidence={})
        before = database.read_bytes()
        assert command(client, receipt, payload, kind="assessment").status_code == 409
        assert database.read_bytes() == before
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == version
            if version == 2:
                assert conn.execute("SELECT staff_write_mode FROM store_metadata").fetchone()[0] == "legacy"


def test_quality_decisions_do_not_open_old_staff_update_paths(quality_api):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    assert command(client, receipt, body(client, receipt, decision="exclude")).status_code == 200
    before = database.read_bytes()
    for action in ("call", "absent", "return", "seat", "cancel"):
        assert client.post(f"/receptions/{receipt['id']}/{action}", headers=STAFF).status_code == 409
        assert database.read_bytes() == before
    current = client.get(f"/staff/receptions/{receipt['id']}/data-quality", headers=STAFF).json()
    assert current["training_use"] == "blocked"


def test_quality_context_does_not_echo_credentials_and_ledger_contains_only_shared_subject(quality_api, caplog):
    lab, database, clock, app, client = quality_api
    receipt = setup_issue(client, app, clock)
    payload = body(client, receipt)
    assert command(client, receipt, payload).status_code == 200
    current = context(client, receipt)
    assert set(current) == {"reception_id", "revision", "write_epoch", "watermarks",
                            "latest_assessment_id", "valid_seated_event_id", "issues"}
    assert TOKEN not in json.dumps(current, ensure_ascii=False)
    assert TOKEN not in caplog.text
    assert TOKEN.encode() not in database.read_bytes()
    latest = table(lab, database, "staff_operations")[-1]
    assert (latest["principal_kind"], latest["principal_id"]) == ("shared_staff_token", "shared-staff")


def test_quality_reviews_leave_same_input_live_predictions_saved_intake_and_actual_wait_unchanged(quality_api):
    lab, database, clock, app, client = quality_api

    class SyntheticEstimator:
        def __init__(self):
            self.inputs = []

        def predict(self, features):
            self.inputs.append((tuple(features.columns), features.to_numpy().tobytes()))
            return np.asarray([17.125])

    estimator = SyntheticEstimator()
    app.state.predictor._artifact = {
        "model": estimator, "metadata": {"model_version": "synthetic-quality-only"},
    }
    receipt = setup_issue(client, app, clock)
    waiting = register(client)
    original = app.state.store.get(receipt["id"])
    waiting_original = app.state.store.get(waiting["id"])
    original_tables = {name: table(lab, database, name) for name in
                       ("reception_snapshots", "predictions", "idempotency_keys", "reception_events")}
    party = {"adults": 2, "children": 0}
    before = client.post("/predict", json=party)
    assert before.status_code == 200
    assert before.json()["prediction"]["status"] == "available"
    assert before.json()["prediction"]["minutes"] == 17.125
    assert before.json()["snapshot"]["waiting_groups"] == 1
    features_before = estimator.inputs[-1]
    calls_before = len(estimator.inputs)
    fixed_clock = clock.value
    assert command(client, receipt, body(client, receipt, decision="hold")).status_code == 200
    assert command(client, receipt, body(client, receipt, decision="exclude")).status_code == 200
    assert command(client, receipt, body(client, receipt, kind="assessment", decision="hold"),
                   kind="assessment").status_code == 200
    assert len(estimator.inputs) == calls_before
    assert clock.value == fixed_clock
    after = client.post("/predict", json=party)
    assert after.status_code == 200 and after.json() == before.json()
    assert estimator.inputs[-1] == features_before
    assert "waiting_groups" not in features_before[0]
    assert len(estimator.inputs) == calls_before + 1
    assert app.state.store.get(receipt["id"]) == original
    assert original["actual_wait_minutes"] == 25
    assert app.state.store.get(waiting["id"]) == waiting_original
    assert {name: table(lab, database, name) for name in original_tables} == original_tables
