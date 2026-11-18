"""Correction HTTP contracts and serving predictions on disposable synthetic DBs."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
import sqlite3
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import numpy as np
import pytest

from prediction_api import create_app
from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1


TOKEN = "synthetic-seat-correction-shared-token-never-persist-123456789"
STAFF = {"X-Staff-Token": TOKEN}
ORIGIN = "http://127.0.0.1"
RECEPTION_KEYS = {
    "id", "display_number", "reception_order", "received_at", "adults", "children",
    "total_people", "status", "called_at", "seated_at", "cancelled_at",
    "actual_wait_minutes", "snapshot", "prediction",
}
RESULT_KEYS = {
    "operation_id", "reception_id", "action", "outcome", "response_status",
    "recorded_at", "resulting_revision", "reception", "error",
}


class Clock:
    def __init__(self):
        # A supported Friday; the synthetic estimator never loads a real model.
        self.value = datetime(2026, 10, 30, 18, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


class SyntheticEstimator:
    def __init__(self, output=None):
        self.output = np.asarray([17.125]) if output is None else output
        self.inputs = []

    def predict(self, features):
        self.inputs.append((tuple(features.columns), features.to_numpy().tobytes()))
        return self.output


def app_for(database, clock):
    return create_app(db_path=database, clock=clock, staff_token=TOKEN,
                      model_path=database.parent / "nonexistent-synthetic-model.joblib")


@contextmanager
def environment(*, version=2, mode="revision_required", enabled=True):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
            with lab.connection(database) as conn:
                conn.execute("UPDATE store_metadata SET staff_write_mode=?,seat_corrections_enabled=?",
                             (mode, int(enabled)))
        clock = Clock()
        app = app_for(database, clock)
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            yield lab, database, clock, client


def register(client, index=1001):
    response = client.post("/receptions", json={"adults": 2, "children": 1},
                           headers={"Idempotency-Key": str(UUID(int=index))})
    assert response.status_code == 201
    assert set(response.json()) == RECEPTION_KEYS
    return response.json()


def current(client, reception_id):
    response = client.get(f"/staff/receptions/{reception_id}", headers=STAFF)
    assert response.status_code == 200
    assert set(response.json()) == {"reception", "revision", "write_epoch", "staff_write_mode"}
    return response.json()


def command(client, reception_id, action, index):
    state = current(client, reception_id)
    body = {"operation_id": str(UUID(int=index)), "action": action,
            "expected_revision": state["revision"], "write_epoch": state["write_epoch"]}
    return client.post(f"/staff/receptions/{reception_id}/operations", json=body, headers=STAFF), body


def events(client, reception_id):
    response = client.get(f"/staff/receptions/{reception_id}/events", headers=STAFF)
    assert response.status_code == 200
    assert set(response.json()) == {"events"}
    result = response.json()["events"]
    assert [event["event_id"] for event in result] == sorted(event["event_id"] for event in result)
    return result


def target_event(client, reception_id):
    return [event for event in events(client, reception_id) if event["event_type"] == "seated"][-1]["event_id"]


def correction_body(client, reception_id, index=3001, **overrides):
    state = current(client, reception_id)
    body = {"operation_id": str(UUID(int=index)), "expected_revision": state["revision"],
            "write_epoch": state["write_epoch"], "target_event_id": target_event(client, reception_id),
            "reason_code": "wrong_reception", "reason_detail": "別のお客様の受付を誤って選択したため"}
    body.update(overrides)
    return body


def correction(client, reception_id, body):
    return client.post(f"/staff/receptions/{reception_id}/seating-corrections", json=body, headers=STAFF)


def assert_result(response, *, status=200, outcome="state_changed"):
    assert response.status_code == status
    value = response.json()
    assert set(value) == RESULT_KEYS
    assert value["action"] == "seating_correction"
    assert value["response_status"] == status and value["outcome"] == outcome
    if status == 200:
        assert value["error"] is None and set(value["reception"]) == RECEPTION_KEYS
    else:
        assert value["reception"] is None and set(value["error"]) == {"code", "detail"}
    return value


def quality(client, reception_id):
    response = client.get(f"/staff/receptions/{reception_id}/data-quality", headers=STAFF)
    assert response.status_code == 200
    value = response.json()
    assert value["reception_id"] == reception_id and value["training_use"] == "blocked"
    assert isinstance(value["issues"], list) and isinstance(value["unresolved_issue_ids"], list)
    assert value["reasons"]
    return value


def stored_originals(lab, database):
    with lab.connection(database, readonly=True) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY reception_id").fetchall()
            for table in ("reception_snapshots", "predictions")
        } | {"cached": conn.execute("SELECT key,reception_id,response_json FROM idempotency_keys ORDER BY key").fetchall()}


@pytest.mark.parametrize("original_status", ["waiting", "called", "absent"])
def test_correction_restores_server_selected_original_status_and_audits_reason(original_status):
    with environment() as (lab, database, clock, client):
        receipt = register(client)
        if original_status != "waiting":
            assert command(client, receipt["id"], "call", 2001)[0].status_code == 200
        if original_status == "absent":
            assert command(client, receipt["id"], "absent", 2002)[0].status_code == 200
        before_seat = current(client, receipt["id"])
        clock.value += timedelta(minutes=10)
        seated, _ = command(client, receipt["id"], "seat", 2003)
        assert seated.status_code == 200 and seated.json()["reception"]["actual_wait_minutes"] == 10
        originals = stored_originals(lab, database)
        body = correction_body(client, receipt["id"])
        old_events = events(client, receipt["id"])
        clock.value += timedelta(minutes=1)
        result = assert_result(correction(client, receipt["id"], body))
        restored = result["reception"]
        assert restored["status"] == original_status
        assert restored["seated_at"] is None and restored["actual_wait_minutes"] is None
        assert restored["called_at"] == before_seat["reception"]["called_at"]
        for field in RECEPTION_KEYS - {"status", "called_at", "seated_at", "actual_wait_minutes"}:
            assert restored[field] == receipt[field]
        assert result["resulting_revision"] == body["expected_revision"] + 1
        new_events = events(client, receipt["id"])
        assert new_events[:-1] == old_events
        assert new_events[-1]["event_type"] == "seating_corrected"
        assert new_events[-1]["corrects_event_id"] == body["target_event_id"]
        assert new_events[-1]["from_status"] == "seated" and new_events[-1]["to_status"] == original_status
        assert new_events[-1]["reason_code"] == body["reason_code"]
        assert new_events[-1]["reason_detail"] == body["reason_detail"]
        assert stored_originals(lab, database) == originals
        held = quality(client, receipt["id"])
        assert "invalidated_seating" in {issue["issue_kind"] for issue in held["issues"]}
        assert held["unresolved_issue_ids"]
        # Initialization must verify a real corrected ledger, not just the live connection.
        with TestClient(app_for(database, clock), base_url=ORIGIN,
                        client=("127.0.0.1", 50000)) as reopened:
            assert current(reopened, receipt["id"])["reception"] == restored
            assert quality(reopened, receipt["id"])["unresolved_issue_ids"] == held["unresolved_issue_ids"]


def test_001_wrong_seat_002_registration_correct_001_then_actual_25_minute_seating():
    with environment() as (lab, database, clock, client):
        first = register(client)
        assert first["display_number"].endswith("001")
        clock.value += timedelta(minutes=10)
        seat_a, seat_a_body = command(client, first["id"], "seat", 2101)
        assert seat_a.status_code == 200 and seat_a.json()["reception"]["actual_wait_minutes"] == 10
        clock.value += timedelta(seconds=30)
        second = register(client, 1002)
        assert second["display_number"].endswith("002")
        assert second["snapshot"]["waiting_groups"] == 0
        assert second["prediction"]["status"] == "no_waiting_groups"
        originals = stored_originals(lab, database)
        body = correction_body(client, first["id"], 3101)
        clock.value += timedelta(seconds=30)
        corrected = correction(client, first["id"], body)
        assert_result(corrected)
        queue = client.get("/queue", headers=STAFF).json()
        assert queue["waiting_groups"] == 2
        assert [record["id"] for record in queue["groups"]] == [first["id"], second["id"]]
        assert client.get("/public/queue").json()["waiting_groups"] == 2
        assert current(client, first["id"])["reception"]["reception_order"] == first["reception_order"]
        # There is no customer-specific ahead-count endpoint: verify the current
        # business aggregate internally without rewriting intake-time snapshots.
        with lab.connection(database, readonly=True) as conn:
            ahead = conn.execute("SELECT COUNT(*) FROM receptions WHERE reception_order<? "
                                 "AND status IN ('waiting','called','absent')",
                                 (second["reception_order"],)).fetchone()[0]
            assert ahead == 1
        first_held, second_held = quality(client, first["id"]), quality(client, second["id"])
        assert "invalidated_seating" in {issue["issue_kind"] for issue in first_held["issues"]}
        assert {"snapshot_context", "prediction_gate"} <= {issue["issue_kind"] for issue in second_held["issues"]}
        # A -> B -> retry A returns A's historical response and cannot seat again.
        after_correction_bytes = database.read_bytes()
        old_a = client.post(f'/staff/receptions/{first["id"]}/operations', json=seat_a_body, headers=STAFF)
        assert old_a.content == seat_a.content
        assert current(client, first["id"])["reception"]["status"] == "waiting"
        assert database.read_bytes() == after_correction_bytes
        # B's response may be lost; identical retries and authenticated lookup
        # return exactly B without additional issue, event, or revision records.
        for _ in range(2):
            replay = correction(client, first["id"], body)
            assert replay.content == corrected.content
            lookup = client.get(f'/staff/operations/{body["operation_id"]}', headers=STAFF)
            assert lookup.status_code == 200 and lookup.json() == corrected.json()
            assert database.read_bytes() == after_correction_bytes
        clock.value += timedelta(minutes=14)
        real_seat, _ = command(client, first["id"], "seat", 2102)
        assert real_seat.status_code == 200
        assert real_seat.json()["reception"]["actual_wait_minutes"] == 25.0
        assert real_seat.json()["reception"]["received_at"] == first["received_at"]
        assert quality(client, first["id"])["unresolved_issue_ids"] == first_held["unresolved_issue_ids"]
        assert quality(client, second["id"])["unresolved_issue_ids"] == second_held["unresolved_issue_ids"]
        assert stored_originals(lab, database) == originals
        replay_registration = client.post("/receptions", json={"adults": 2, "children": 1},
                                          headers={"Idempotency-Key": str(UUID(int=1001))})
        assert replay_registration.status_code == 200 and replay_registration.json() == first
        all_events = events(client, first["id"])
        assert [event["event_type"] for event in all_events] == ["registered", "seated", "seating_corrected", "seated"]
        assert len({event["event_id"] for event in all_events}) == 4


@pytest.mark.parametrize("endpoint", ["correction", "events", "quality", "lookup"])
@pytest.mark.parametrize("headers", [{}, {"X-Staff-Token": "incorrect-synthetic-token"}])
def test_all_correction_history_quality_and_replay_endpoints_require_staff_auth(endpoint, headers):
    with environment() as (_, database, _, client):
        receipt = register(client)
        assert command(client, receipt["id"], "seat", 2201)[0].status_code == 200
        body = correction_body(client, receipt["id"])
        baseline = database.read_bytes()
        if endpoint == "correction":
            response = client.post(f'/staff/receptions/{receipt["id"]}/seating-corrections', json=body, headers=headers)
        else:
            path = {"events": f'/staff/receptions/{receipt["id"]}/events',
                    "quality": f'/staff/receptions/{receipt["id"]}/data-quality',
                    "lookup": f'/staff/operations/{body["operation_id"]}'}[endpoint]
            response = client.get(path, headers=headers)
        assert response.status_code == 401
        assert database.read_bytes() == baseline
        assert TOKEN not in response.text


@pytest.mark.parametrize("version,mode,enabled", [(1, "legacy", False), (2, "legacy", False),
                                                (2, "revision_required", False)])
def test_correction_is_rejected_without_explicit_v2_required_mode_and_feature_flag(version, mode, enabled):
    with environment(version=version, mode=mode, enabled=enabled) as (lab, database, _, client):
        receipt = register(client)
        if mode == "legacy":
            assert client.post(f'/receptions/{receipt["id"]}/seat', headers=STAFF).status_code == 200
        else:
            assert command(client, receipt["id"], "seat", 2301)[0].status_code == 200
        with lab.connection(database, readonly=True) as conn:
            target = conn.execute("SELECT MAX(event_id) FROM reception_events WHERE event_type='seated'").fetchone()[0]
            epoch = (conn.execute("SELECT write_epoch FROM store_metadata").fetchone()[0]
                     if version == 2 else str(UUID(int=9999)))
        body = {"operation_id": str(UUID(int=3301)), "expected_revision": int(version == 2),
                "write_epoch": epoch, "target_event_id": target,
                "reason_code": "other", "reason_detail": "合成DBの機能無効テスト"}
        baseline = database.read_bytes()
        response = correction(client, receipt["id"], body)
        assert response.status_code == 409
        if version == 2:
            assert current(client, receipt["id"])["reception"]["status"] == "seated"
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT status FROM receptions").fetchone() == ("seated",)
            assert conn.execute("SELECT COUNT(*) FROM reception_events WHERE event_type='seating_corrected'").fetchone() == (0,)
        # A disabled feature must not create new quality records or mutate state.
        # Persisting a protected rejection in v2 is permitted by the command protocol.
        if version == 1:
            assert database.read_bytes() == baseline


@pytest.mark.parametrize("field,value", [
    ("operation_id", str(UUID(int=0))), ("operation_id", "not-a-uuid"),
    ("write_epoch", str(UUID(int=0))), ("expected_revision", -1),
    ("expected_revision", True), ("expected_revision", 1.0),
    ("expected_revision", 2**63), ("target_event_id", 0),
    ("target_event_id", -1), ("target_event_id", True),
    ("target_event_id", 1.0), ("target_event_id", 2**63),
    ("reason_code", "unsupported"), ("reason_detail", ""),
    ("reason_detail", "  \n\t"), ("reason_detail", "x" * 501),
    ("reason_detail", None), ("to_status", "waiting"),
    ("action", "seat"), ("status", "waiting"),
])
def test_invalid_correction_body_is_rejected_before_any_database_write(field, value):
    with environment() as (_, database, _, client):
        receipt = register(client)
        assert command(client, receipt["id"], "seat", 2401)[0].status_code == 200
        body = correction_body(client, receipt["id"])
        body[field] = value
        baseline = database.read_bytes()
        response = correction(client, receipt["id"], body)
        assert response.status_code == 422
        assert database.read_bytes() == baseline
        assert TOKEN not in response.text


@pytest.mark.parametrize("field", ["operation_id", "expected_revision", "write_epoch",
                                    "target_event_id", "reason_code", "reason_detail"])
def test_every_correction_protection_and_reason_field_is_required(field):
    with environment() as (_, database, _, client):
        receipt = register(client)
        assert command(client, receipt["id"], "seat", 2501)[0].status_code == 200
        body = correction_body(client, receipt["id"])
        del body[field]
        baseline = database.read_bytes()
        assert correction(client, receipt["id"], body).status_code == 422
        assert database.read_bytes() == baseline


@pytest.mark.parametrize("changed", ["target_event_id", "reason_code", "reason_detail",
                                      "expected_revision", "write_epoch", "reception_id"])
def test_correction_operation_id_cannot_be_reused_for_different_content(changed):
    with environment() as (_, database, _, client):
        first, second = register(client), register(client, 1002)
        assert command(client, first["id"], "seat", 2601)[0].status_code == 200
        body = correction_body(client, first["id"])
        saved = correction(client, first["id"], body)
        assert_result(saved)
        target = first["id"]
        if changed == "reception_id":
            target = second["id"]
        else:
            body[changed] = {"target_event_id": body["target_event_id"] + 1,
                             "reason_code": "accidental_seating", "reason_detail": "変更した訂正理由",
                             "expected_revision": body["expected_revision"] + 1,
                             "write_epoch": str(UUID(int=9999))}[changed]
        baseline = database.read_bytes()
        assert correction(client, target, body).status_code == 409
        assert database.read_bytes() == baseline
        assert client.get(f'/staff/operations/{body["operation_id"]}', headers=STAFF).json() == saved.json()


@pytest.mark.parametrize("mismatch", ["revision", "epoch", "other_reception_event", "registration_event", "nonexistent_event"])
def test_invalid_target_or_concurrency_token_is_persisted_as_safe_replayable_rejection(mismatch):
    with environment() as (_, database, _, client):
        first, second = register(client), register(client, 1002)
        assert command(client, first["id"], "seat", 2701)[0].status_code == 200
        assert command(client, second["id"], "seat", 2702)[0].status_code == 200
        body = correction_body(client, first["id"])
        if mismatch == "revision":
            body["expected_revision"] -= 1
        elif mismatch == "epoch":
            body["write_epoch"] = str(UUID(int=9999))
        elif mismatch == "other_reception_event":
            body["target_event_id"] = target_event(client, second["id"])
        elif mismatch == "registration_event":
            body["target_event_id"] = events(client, first["id"])[0]["event_id"]
        else:
            body["target_event_id"] = 999999
        before = current(client, first["id"])
        response = correction(client, first["id"], body)
        assert_result(response, status=409, outcome="rejected")
        assert current(client, first["id"]) == before
        baseline = database.read_bytes()
        assert correction(client, first["id"], body).content == response.content
        assert client.get(f'/staff/operations/{body["operation_id"]}', headers=STAFF).json() == response.json()
        assert database.read_bytes() == baseline


def test_corrected_event_cannot_be_corrected_again_after_reseating_even_with_current_revision():
    with environment() as (_, _, _, client):
        receipt = register(client)
        assert command(client, receipt["id"], "seat", 2801)[0].status_code == 200
        body = correction_body(client, receipt["id"])
        assert_result(correction(client, receipt["id"], body))
        assert command(client, receipt["id"], "seat", 2802)[0].status_code == 200
        old_target = body["target_event_id"]
        stale = correction_body(client, receipt["id"], 3801, target_event_id=old_target)
        before = current(client, receipt["id"])
        assert_result(correction(client, receipt["id"], stale), status=409, outcome="rejected")
        assert current(client, receipt["id"]) == before


def test_seating_operation_id_cannot_be_reused_for_a_correction_command():
    with environment() as (_, database, _, client):
        receipt = register(client)
        seated, seat_body = command(client, receipt["id"], "seat", 2851)
        assert seated.status_code == 200
        body = correction_body(client, receipt["id"], operation_id=seat_body["operation_id"])
        baseline = database.read_bytes()
        assert correction(client, receipt["id"], body).status_code == 409
        assert database.read_bytes() == baseline
        assert client.get(f'/staff/operations/{seat_body["operation_id"]}', headers=STAFF).json() == seated.json()


def test_uuid_normalization_does_not_turn_an_identical_correction_retry_into_a_new_operation():
    with environment() as (_, database, _, client):
        receipt = register(client)
        assert command(client, receipt["id"], "seat", 2861)[0].status_code == 200
        body = correction_body(client, receipt["id"], operation_id="11111111-aaaa-4444-bbbb-222222222222")
        body["operation_id"], body["write_epoch"] = body["operation_id"].upper(), body["write_epoch"].upper()
        result = correction(client, receipt["id"], body)
        assert_result(result)
        assert result.json()["operation_id"] == body["operation_id"].lower()
        baseline = database.read_bytes()
        body["operation_id"], body["write_epoch"] = body["operation_id"].lower(), body["write_epoch"].lower()
        assert correction(client, receipt["id"], body).content == result.content
        assert database.read_bytes() == baseline


def test_quality_storage_failure_returns_503_and_rolls_back_state_event_operation_and_every_issue(monkeypatch):
    with environment() as (lab, database, clock, client):
        first = register(client)
        clock.value += timedelta(minutes=10)
        assert command(client, first["id"], "seat", 2871)[0].status_code == 200
        register(client, 1002)
        clock.value += timedelta(minutes=1)
        body = correction_body(client, first["id"])
        before = current(client, first["id"])
        baseline = database.read_bytes()
        original = client.app.state.store._save_quality_issues

        def fail_after_quality_save(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("synthetic failure after quality issue writes")

        with monkeypatch.context() as patch:
            patch.setattr(client.app.state.store, "_save_quality_issues", fail_after_quality_save)
            response = correction(client, first["id"], body)
        assert response.status_code == 503 and response.headers["Retry-After"] == "1"
        assert "synthetic failure" not in response.text
        assert current(client, first["id"]) == before
        assert database.read_bytes() == baseline
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM reception_events WHERE event_type='seating_corrected'").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM staff_operations WHERE operation_id=?",
                                (body["operation_id"],)).fetchone() == (0,)
        # An uncommitted failed attempt did not consume the UUID; the exact
        # original command can be retried successfully after storage recovers.
        assert_result(correction(client, first["id"], body))


def test_old_staff_routes_and_unprotected_correction_action_cannot_bypass_protection():
    with environment() as (_, database, _, client):
        receipt = register(client)
        assert command(client, receipt["id"], "seat", 2901)[0].status_code == 200
        assert_result(correction(client, receipt["id"], correction_body(client, receipt["id"])))
        baseline = database.read_bytes()
        for action in ("call", "absent", "return", "seat", "cancel"):
            response = client.post(f'/receptions/{receipt["id"]}/{action}', headers=STAFF)
            assert response.status_code == 409
            assert database.read_bytes() == baseline
        state = current(client, receipt["id"])
        response = client.post(f'/staff/receptions/{receipt["id"]}/operations', headers=STAFF, json={
            "operation_id": str(UUID(int=3901)), "action": "seating_correction",
            "expected_revision": state["revision"], "write_epoch": state["write_epoch"],
        })
        assert response.status_code == 422 and database.read_bytes() == baseline


def test_quality_without_issues_is_still_blocked_until_a_safe_training_extractor_exists():
    with environment() as (_, _, _, client):
        receipt = register(client)
        status = quality(client, receipt["id"])
        assert status["issues"] == [] and status["unresolved_issue_ids"] == []
        assert command(client, receipt["id"], "seat", 2902)[0].status_code == 200
        assert quality(client, receipt["id"])["training_use"] == "blocked"


def test_prediction_gate_recovers_after_correction_without_changing_features_or_saved_predictions():
    with environment() as (lab, database, _, client):
        estimator = SyntheticEstimator()
        client.app.state.predictor._artifact = {"model": estimator, "metadata": {"model_version": "synthetic-only"}}
        receipt = register(client)
        party = {"adults": 2, "children": 1}
        before = client.post("/predict", json=party).json()
        assert before["prediction"]["status"] == "available"
        first_features = estimator.inputs[-1]
        originals = stored_originals(lab, database)
        assert command(client, receipt["id"], "seat", 4001)[0].status_code == 200
        calls_before_suppression = len(estimator.inputs)
        suppressed = client.post("/predict", json=party).json()
        assert suppressed["prediction"]["status"] == "no_waiting_groups"
        assert suppressed["prediction"]["minutes"] is None and suppressed["snapshot"]["waiting_groups"] == 0
        assert len(estimator.inputs) == calls_before_suppression
        assert_result(correction(client, receipt["id"], correction_body(client, receipt["id"])))
        recovered = client.post("/predict", json=party).json()
        repeated = client.post("/predict", json=party).json()
        assert recovered == before == repeated
        assert estimator.inputs[-1] == first_features
        assert "waiting_groups" not in estimator.inputs[-1][0]
        assert command(client, receipt["id"], "call", 4002)[0].status_code == 200
        assert command(client, receipt["id"], "absent", 4003)[0].status_code == 200
        assert client.post("/predict", json=party).json() == before
        assert stored_originals(lab, database) == originals
        # More queued groups can change the gate/context, but this estimator's
        # learned feature contract must receive exactly the same input features.
        register(client, 1002)
        with_two = client.post("/predict", json=party).json()
        assert with_two["snapshot"]["waiting_groups"] == 2
        assert with_two["prediction"] == before["prediction"]
        assert estimator.inputs[-1] == first_features


@pytest.mark.parametrize("output", [np.asarray([np.nan]), np.asarray([np.inf]),
                                    np.asarray([-np.inf]), np.asarray([-1.0]),
                                    np.asarray([[17.0]]), np.asarray([]), np.asarray(["invalid"])])
def test_invalid_estimator_output_is_not_exposed_as_a_normal_prediction_after_correction(output):
    with environment() as (lab, database, _, client):
        receipt = register(client)
        assert command(client, receipt["id"], "seat", 4101)[0].status_code == 200
        assert_result(correction(client, receipt["id"], correction_body(client, receipt["id"])))
        originals = stored_originals(lab, database)
        estimator = SyntheticEstimator(output)
        client.app.state.predictor._artifact = {"model": estimator, "metadata": {"model_version": "synthetic-only"}}
        response = client.post("/predict", json={"adults": 2, "children": 1})
        assert response.status_code == 200
        assert response.json()["prediction"]["status"] == "prediction_failed"
        assert response.json()["prediction"]["minutes"] is None
        assert "NaN" not in response.text and "Infinity" not in response.text
        assert stored_originals(lab, database) == originals


def test_correction_metadata_and_shared_credentials_never_enter_customer_responses():
    with environment() as (lab, database, _, client):
        receipt = register(client)
        assert command(client, receipt["id"], "seat", 4201)[0].status_code == 200
        body = correction_body(client, receipt["id"])
        assert_result(correction(client, receipt["id"], body))
        public_outputs = [client.get("/public/queue").json(), client.get("/health").json(),
                          client.post("/predict", json={"adults": 2, "children": 1}).json()]
        replay = client.post("/receptions", json={"adults": 2, "children": 1},
                             headers={"Idempotency-Key": str(UUID(int=1001))})
        assert replay.json() == receipt
        public_outputs.append(replay.json())
        encoded = json.dumps(public_outputs, ensure_ascii=False)
        for secret in (TOKEN, body["operation_id"], body["write_epoch"], body["reason_detail"]):
            assert secret not in encoded
        for internal in ("revision", "operation_id", "write_epoch", "reason_code", "reason_detail",
                         "corrects_event_id", "data_quality_issues", "principal_kind", "training_use"):
            assert f'"{internal}"' not in encoded
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT DISTINCT principal_kind,principal_id FROM staff_operations").fetchall() == [
                ("shared_staff_token", "shared-staff")]
            for table in ("staff_operations", "reception_events", "data_quality_issues"):
                assert TOKEN not in repr(conn.execute(f"SELECT * FROM {table}").fetchall())
