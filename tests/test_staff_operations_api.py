"""Protected staff HTTP commands use disposable, synthetic databases only."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from prediction_api import create_app
from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1


TOKEN = "synthetic-protected-staff-token-never-persist-123456789"
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
        self.value = datetime(2026, 10, 29, 12, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


def app_for(database, clock, *, token=TOKEN):
    return create_app(db_path=database, clock=clock, staff_token=token,
                      model_path=database.parent / "nonexistent-synthetic-model.joblib")


@contextmanager
def environment(*, version=2, mode="legacy"):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
            if mode != "legacy":
                with lab.connection(database) as conn:
                    conn.execute("UPDATE store_metadata SET staff_write_mode=?", (mode,))
        clock = Clock()
        app = app_for(database, clock)
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            yield lab, database, clock, client


def register(client, key=1001):
    response = client.post("/receptions", json={"adults": 2, "children": 1},
                           headers={"Idempotency-Key": str(UUID(int=key))})
    assert response.status_code == 201
    assert set(response.json()) == RECEPTION_KEYS
    return response.json()


def current(client, reception_id):
    response = client.get(f"/staff/receptions/{reception_id}", headers=STAFF)
    assert response.status_code == 200
    value = response.json()
    assert set(value) == {"reception", "revision", "write_epoch", "staff_write_mode"}
    assert set(value["reception"]) == RECEPTION_KEYS
    return value


def command(client, reception_id, command_action, operation_index, *, state=None, **overrides):
    state = current(client, reception_id) if state is None else state
    body = {"operation_id": str(UUID(int=operation_index)), "action": command_action,
            "expected_revision": state["revision"], "write_epoch": state["write_epoch"]}
    body.update(overrides)
    response = client.post(f"/staff/receptions/{reception_id}/operations", json=body, headers=STAFF)
    return response, body


def assert_envelope(response, *, status=200, outcome="state_changed"):
    assert response.status_code == status
    result = response.json()
    assert set(result) == RESULT_KEYS
    assert result["response_status"] == status
    assert result["outcome"] == outcome
    if result["reception"] is not None:
        assert set(result["reception"]) == RECEPTION_KEYS
    if status == 200:
        assert result["error"] is None
    else:
        assert set(result["error"]) == {"code", "detail"}
        assert result["error"]["code"]
    return result


@pytest.mark.parametrize("mode", ["legacy", "revision_required"])
def test_all_five_actions_use_revision_and_persist_shared_authenticated_audit(mode):
    with environment(mode=mode) as (lab, database, clock, client):
        receipt = register(client)
        initial = current(client, receipt["id"])
        assert initial["revision"] == 0 and initial["staff_write_mode"] == mode
        assert initial["reception"] == receipt
        for revision, (action, status) in enumerate([
            ("call", "called"), ("absent", "absent"), ("return", "waiting"),
            ("call", "called"), ("seat", "seated"),
        ], 1):
            clock.value += timedelta(seconds=15, microseconds=500000)
            response, body = command(client, receipt["id"], action, 2000 + revision)
            result = assert_envelope(response)
            assert result["operation_id"] == body["operation_id"]
            assert result["reception_id"] == receipt["id"] and result["action"] == action
            assert result["resulting_revision"] == revision
            assert result["reception"]["status"] == status
            for field in ("id", "display_number", "reception_order", "received_at", "snapshot", "prediction"):
                assert result["reception"][field] == receipt[field]
            assert current(client, receipt["id"])["revision"] == revision
        assert result["reception"]["actual_wait_minutes"] == pytest.approx(77.5 / 60)
        cancelled = register(client, 1002)
        cancellation, _ = command(client, cancelled["id"], "cancel", 2010)
        assert assert_envelope(cancellation)["reception"]["status"] == "cancelled"
        assert cancellation.json()["reception"]["actual_wait_minutes"] is None
        with lab.connection(database, readonly=True) as conn:
            operations = conn.execute("SELECT principal_kind,principal_id,outcome FROM staff_operations").fetchall()
            assert operations == [("shared_staff_token", "shared-staff", "state_changed")] * 6
            events = conn.execute("SELECT audit_kind,operation_id,revision_before,revision_after "
                                  "FROM reception_events WHERE operation_id IS NOT NULL ORDER BY event_id").fetchall()
            assert len(events) == 6 and all(event[0] == "staff_v2" for event in events)
            assert [(row[2], row[3]) for row in events] == [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (0, 1)]
            assert conn.execute("SELECT seat_corrections_enabled FROM store_metadata").fetchone() == (0,)


def test_lost_seating_response_retry_result_lookup_and_reopen_do_not_repeat_seating():
    with environment() as (lab, database, clock, client):
        receipt = register(client)
        clock.value += timedelta(seconds=30, microseconds=123456)
        response, body = command(client, receipt["id"], "seat", 2100)
        saved = assert_envelope(response)
        original_bytes = database.read_bytes()
        for _ in range(3):
            clock.value += timedelta(minutes=10)
            retried = client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=STAFF)
            assert retried.status_code == 200 and retried.content == response.content
            lookup = client.get(f'/staff/operations/{body["operation_id"]}', headers=STAFF)
            assert lookup.status_code == 200 and lookup.json() == saved
            assert database.read_bytes() == original_bytes
        with TestClient(app_for(database, clock), base_url=ORIGIN,
                        client=("127.0.0.1", 50000)) as reopened:
            retried = reopened.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=STAFF)
            assert retried.status_code == 200 and retried.json() == saved
            assert current(reopened, receipt["id"])["revision"] == 1
        assert database.read_bytes() == original_bytes
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM staff_operations").fetchone() == (1,)
            assert conn.execute("SELECT COUNT(*) FROM reception_events WHERE event_type='seated'").fetchone() == (1,)
            assert conn.execute("SELECT actual_wait_minutes FROM receptions").fetchone()[0] == pytest.approx(0.5020576)


@pytest.mark.parametrize("changed", ["action", "reception_id", "expected_revision", "write_epoch"])
def test_operation_id_cannot_be_reused_with_a_different_command(changed):
    with environment() as (_, database, _, client):
        first, second = register(client), register(client, 1002)
        response, body = command(client, first["id"], "call", 2200)
        assert_envelope(response)
        target = first["id"]
        if changed == "reception_id":
            target = second["id"]
        else:
            body[changed] = {"action": "seat", "expected_revision": 1,
                             "write_epoch": str(UUID(int=9000))}[changed]
        original = database.read_bytes()
        conflict = client.post(f"/staff/receptions/{target}/operations", json=body, headers=STAFF)
        assert conflict.status_code == 409
        assert database.read_bytes() == original
        lookup = client.get(f'/staff/operations/{body["operation_id"]}', headers=STAFF)
        assert lookup.json() == response.json()


@pytest.mark.parametrize("action", ["call", "absent", "return", "seat", "cancel"])
def test_stale_revision_is_rejected_for_every_action_and_rejection_is_replayable(action):
    with environment(mode="revision_required") as (lab, database, _, client):
        receipt = register(client)
        stale = current(client, receipt["id"])
        assert command(client, receipt["id"], "call", 2300)[0].status_code == 200
        current_state = current(client, receipt["id"])
        response, body = command(client, receipt["id"], action, 2301, state=stale)
        result = assert_envelope(response, status=409, outcome="rejected")
        assert current(client, receipt["id"]) == current_state
        original = database.read_bytes()
        retry = client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=STAFF)
        assert retry.status_code == 409 and retry.json() == result
        lookup = client.get(f'/staff/operations/{body["operation_id"]}', headers=STAFF)
        assert lookup.status_code == 200 and lookup.json() == result
        assert database.read_bytes() == original
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM reception_events").fetchone() == (2,)
            assert conn.execute("SELECT outcome FROM staff_operations ORDER BY rowid").fetchall() == [
                ("state_changed",), ("rejected",)]


def test_fresh_matching_noop_is_audited_without_event_or_revision_increment():
    with environment() as (lab, database, _, client):
        receipt = register(client)
        assert command(client, receipt["id"], "call", 2400)[0].status_code == 200
        before = current(client, receipt["id"])
        response, _ = command(client, receipt["id"], "call", 2401)
        result = assert_envelope(response, outcome="no_change")
        assert result["resulting_revision"] == 1 and result["reception"] == before["reception"]
        assert current(client, receipt["id"]) == before
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM reception_events").fetchone() == (2,)
            assert conn.execute("SELECT outcome FROM staff_operations ORDER BY rowid").fetchall() == [
                ("state_changed",), ("no_change",)]


@pytest.mark.parametrize("action", ["call", "absent", "return", "seat", "cancel"])
def test_old_write_epoch_cannot_modify_any_staff_action(action):
    with environment() as (lab, database, _, client):
        receipt = register(client)
        before = current(client, receipt["id"])
        response, _ = command(client, receipt["id"], action, 2500,
                              write_epoch=str(UUID(int=987654)))
        assert_envelope(response, status=409, outcome="rejected")
        assert current(client, receipt["id"]) == before
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM reception_events").fetchone() == (1,)
            assert conn.execute("SELECT COUNT(*) FROM staff_operations WHERE outcome='rejected'").fetchone() == (1,)


def test_invalid_transition_and_unknown_reception_are_persisted_without_fake_events():
    with environment() as (lab, database, _, client):
        receipt = register(client)
        response, _ = command(client, receipt["id"], "absent", 2600)
        assert_envelope(response, status=409, outcome="rejected")
        state = current(client, receipt["id"])
        missing_id = str(UUID(int=777777))
        unknown, body = command(client, missing_id, "call", 2601, state=state)
        result = assert_envelope(unknown, status=404, outcome="rejected")
        assert result["reception"] is None and result["resulting_revision"] is None
        assert client.get(f'/staff/operations/{body["operation_id"]}', headers=STAFF).json() == result
        assert current(client, receipt["id"]) == state
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM reception_events").fetchone() == (1,)
            assert conn.execute("SELECT resolved_reception_id FROM staff_operations WHERE operation_id=?",
                                (body["operation_id"],)).fetchone() == (None,)


def test_legacy_endpoints_are_closed_in_required_mode_even_for_a_noop():
    with environment(mode="revision_required") as (_, database, _, client):
        receipt = register(client)
        original = database.read_bytes()
        for action in ("call", "absent", "return", "seat", "cancel"):
            response = client.post(f'/receptions/{receipt["id"]}/{action}', headers=STAFF)
            assert response.status_code == 409
            assert database.read_bytes() == original
        assert command(client, receipt["id"], "call", 2700)[0].status_code == 200


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_write_contract_unchanged_and_customer_payloads_never_gain_internal_fields(version):
    with environment(version=version) as (_, _, _, client):
        receipt = register(client)
        result = client.post(f'/receptions/{receipt["id"]}/call', headers=STAFF)
        assert result.status_code == 200 and set(result.json()) == RECEPTION_KEYS
        if version == 2:
            assert current(client, receipt["id"])["revision"] == 1
            assert command(client, receipt["id"], "seat", 2800)[0].status_code == 200
        responses = [client.get("/health"), client.get("/public/queue"),
                     client.post("/predict", json={"adults": 2, "children": 0}),
                     client.get("/queue", headers=STAFF),
                     client.post("/receptions", json={"adults": 2, "children": 1},
                                 headers={"Idempotency-Key": str(UUID(int=1001))})]
        private = ("revision", "write_epoch", "operation_id", "principal_id", "principal_kind",
                   "request_sha256", "staff_write_mode", "database_id", "audit_kind")
        for response in responses:
            assert response.status_code == 200
            assert not any(f'"{field}"' in response.text for field in private)
        assert responses[-1].json() == receipt


def test_v1_rejects_new_staff_protocol_without_schema_or_data_changes():
    with environment(version=1) as (_, database, _, client):
        receipt = register(client)
        original = database.read_bytes()
        assert client.get(f'/staff/receptions/{receipt["id"]}', headers=STAFF).status_code == 409
        assert client.get(f'/staff/operations/{UUID(int=2900)}', headers=STAFF).status_code == 409
        response, _ = command(client, receipt["id"], "call", 2900,
                              state={"revision": 0, "write_epoch": str(UUID(int=8888))})
        assert response.status_code == 409
        assert database.read_bytes() == original


@pytest.mark.parametrize("headers", [{}, {"X-Staff-Token": "invalid-secret-never-reflect"}])
def test_staff_detail_command_retry_and_result_lookup_always_authenticate(headers):
    with environment() as (_, database, _, client):
        receipt = register(client)
        successful, body = command(client, receipt["id"], "call", 3000)
        assert successful.status_code == 200
        original = database.read_bytes()
        for response in [
            client.get(f'/staff/receptions/{receipt["id"]}', headers=headers),
            client.get(f'/staff/operations/{body["operation_id"]}', headers=headers),
            client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=headers),
        ]:
            assert response.status_code == 401
            assert TOKEN not in response.text and "invalid-secret-never-reflect" not in response.text
        assert database.read_bytes() == original


def test_unconfigured_staff_auth_disables_new_endpoints(monkeypatch):
    monkeypatch.delenv("OMACHI_STAFF_TOKEN", raising=False)
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, populated=False)
        migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        with TestClient(app_for(database, Clock(), token=None), base_url=ORIGIN,
                        client=("127.0.0.1", 50000)) as client:
            receipt = register(client)
            original = database.read_bytes()
            body = {"operation_id": str(UUID(int=3100)), "action": "call", "expected_revision": 0,
                    "write_epoch": str(UUID(int=8000))}
            for response in [client.get(f'/staff/receptions/{receipt["id"]}', headers=STAFF),
                             client.get(f'/staff/operations/{UUID(int=3100)}', headers=STAFF),
                             client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=STAFF)]:
                assert response.status_code == 503
            assert database.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("expected_revision", True), ("expected_revision", "0"), ("expected_revision", 0.0),
    ("expected_revision", -1), ("expected_revision", 2 ** 63),
    ("operation_id", "not-a-uuid"), ("write_epoch", "not-a-uuid"),
    ("operation_id", str(UUID(int=0))), ("write_epoch", str(UUID(int=0))),
    ("action", "seating_correction"), ("action", "seated"),
    ("staff_token", "extra-sensitive-input-never-reflect"),
])
def test_malformed_or_extra_input_is_rejected_before_any_audit_mutation(field, value):
    with environment() as (_, database, _, client):
        receipt = register(client)
        state = current(client, receipt["id"])
        original = database.read_bytes()
        response, _ = command(client, receipt["id"], "call", 3200, state=state, **{field: value})
        assert response.status_code == 422
        assert "extra-sensitive-input-never-reflect" not in response.text
        assert database.read_bytes() == original


def test_equivalent_uuid_spelling_replays_the_same_canonical_command():
    with environment() as (_, database, _, client):
        receipt = register(client)
        response, body = command(client, receipt["id"], "call", 0xABCDEF)
        assert response.status_code == 200
        original = database.read_bytes()
        body["operation_id"] = UUID(body["operation_id"]).hex.upper()
        body["write_epoch"] = "{" + body["write_epoch"].upper() + "}"
        retried = client.post(f'/staff/receptions/{receipt["id"].upper()}/operations', json=body, headers=STAFF)
        assert retried.status_code == 200 and retried.json() == response.json()
        assert database.read_bytes() == original


def test_unknown_result_is_404_and_shared_auth_secret_never_persisted_or_logged(caplog):
    with environment() as (lab, database, _, client):
        receipt = register(client)
        success, _ = command(client, receipt["id"], "call", 3300)
        assert success.status_code == 200
        original = database.read_bytes()
        assert client.get(f'/staff/operations/{UUID(int=3301)}', headers=STAFF).status_code == 404
        assert database.read_bytes() == original
        with lab.connection(database, readonly=True) as conn:
            dump = "\n".join(conn.iterdump())
            assert TOKEN not in dump and sha256(TOKEN.encode()).hexdigest() not in dump
            row = conn.execute("SELECT request_json,request_sha256,response_json FROM staff_operations").fetchone()
            assert sha256(row[0].encode("utf-8")).hexdigest() == row[1]
            request, saved = json.loads(row[0]), json.loads(row[2])
            assert "token" not in json.dumps(request).lower()
            assert saved == success.json()
        assert TOKEN not in caplog.text


@pytest.mark.parametrize("restriction", ["origin", "host", "remote"])
def test_staff_protocol_obeys_existing_network_boundary(restriction):
    with environment() as (_, database, clock, client):
        receipt = register(client)
        original = database.read_bytes()
        headers = dict(STAFF)
        if restriction == "origin":
            headers["Origin"] = "https://other.example"
            response = client.get(f'/staff/receptions/{receipt["id"]}', headers=headers)
        elif restriction == "host":
            headers["Host"] = "other.example"
            response = client.get(f'/staff/receptions/{receipt["id"]}', headers=headers)
        else:
            with TestClient(app_for(database, clock), base_url=ORIGIN,
                            client=("192.0.2.1", 50000)) as remote:
                response = remote.get(f'/staff/receptions/{receipt["id"]}', headers=headers)
        assert response.status_code == (400 if restriction == "host" else 403)
        assert database.read_bytes() == original
