"""Reject damaged command ledgers while preserving synthetic databases unchanged."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from prediction_api import create_app
from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_schema import validate_schema
from reception_store import ReceptionStore, StoreValidationError


TOKEN = "synthetic-history-review-shared-token-never-persist-123456789"
HEADERS = {"X-Staff-Token": TOKEN}


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 29, 12, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


@contextmanager
def api_history():
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, "ledger.sqlite3", populated=False)
        migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        with lab.connection(database) as conn:
            conn.execute("UPDATE store_metadata SET staff_write_mode='revision_required'")
        clock = Clock()
        app = create_app(db_path=database, staff_token=TOKEN, clock=clock,
                         model_path=database.parent / "missing-synthetic-model.joblib")
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
            initial = client.post("/receptions", json={"adults": 2, "children": 0},
                                  headers={"Idempotency-Key": str(UUID(int=1000))})
            assert initial.status_code == 201
            rid = initial.json()["id"]
            version = client.get(f"/staff/receptions/{rid}", headers=HEADERS).json()
            operations = {}
            for index, (name, action, revision, seconds, status) in enumerate([
                    ("call", "call", 0, 30, 200), ("noop", "call", 1, 15, 200),
                    ("seat", "seat", 1, 30, 200), ("rejected", "seat", 0, 15, 409)], 2000):
                clock.value += timedelta(seconds=seconds)
                op_id = str(UUID(int=index))
                response = client.post(f"/staff/receptions/{rid}/operations", headers=HEADERS,
                                       json={"operation_id": op_id, "action": action,
                                             "expected_revision": revision, "write_epoch": version["write_epoch"]})
                assert response.status_code == status
                operations[name] = op_id
        # The original, unmodified history must pass the production startup check.
        ReceptionStore(database, clock=clock)
        yield lab, database, clock, operations


def change_operation(lab, database, operation_id, mutate):
    """Restore the exact trigger DDL so schema signature checks cannot mask history bugs."""
    with lab.connection(database) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                               "AND name='staff_operations_no_update'").fetchone()[0]
        cursor = conn.execute("SELECT * FROM staff_operations WHERE operation_id=?", (operation_id,))
        operation = dict(zip([column[0] for column in cursor.description], cursor.fetchone()))
        mutate(operation)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TRIGGER staff_operations_no_update")
            conn.execute("UPDATE staff_operations SET request_json=?,request_sha256=?,response_json=?,recorded_at=? "
                         "WHERE operation_id=?", (operation["request_json"], operation["request_sha256"],
                         operation["response_json"], operation["recorded_at"], operation_id))
            conn.execute(trigger)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        validate_schema(conn, 2, allow_revision_required=True)


def corrupt_request(operation, field):
    request = json.loads(operation["request_json"])
    request[field] = {"reception_id": str(uuid4()), "action": "cancel", "write_epoch": str(uuid4())}[field]
    operation["request_json"] = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    operation["request_sha256"] = sha256(operation["request_json"].encode()).hexdigest()


def corrupt_response(operation, case):
    response = json.loads(operation["response_json"])
    receipt = response["reception"]
    if case == "response_reception_id":
        receipt["id"] = str(uuid4())
    elif case == "response_internal_key":
        receipt["revision"] = response["resulting_revision"]
    elif case in {"response_historical_state", "noop_historical_state"}:
        receipt["called_at"] = receipt["received_at"]
    elif case == "response_actual_wait":
        receipt["actual_wait_minutes"] += 10.0
    elif case == "response_snapshot":
        receipt["snapshot"]["waiting_groups"] += 1
    elif case == "response_prediction":
        receipt["prediction"]["warnings"].append("corrupted immutable prediction")
    elif case == "rejected_shape":
        del response["error"]["detail"]
    elif case == "rejected_error_type":
        response["error"]["code"] = 123
    elif case == "noop_after_next_revision":
        # Original no-op: 12:00:45 revision 1. Seating: 12:01:15 revision 2.
        # This later timestamp would mean an old revision incorrectly succeeded.
        later = datetime.fromisoformat(response["recorded_at"]) + timedelta(seconds=35)
        response["recorded_at"] = operation["recorded_at"] = later.isoformat()
    else:
        raise AssertionError(case)
    operation["response_json"] = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("case", [
    "request_reception_id", "request_action", "request_write_epoch",
    "response_reception_id", "response_internal_key", "response_historical_state",
    "response_actual_wait", "response_snapshot", "response_prediction",
    "noop_historical_state", "rejected_shape", "rejected_error_type", "noop_after_next_revision",
])
def test_damaged_archived_request_or_response_fails_before_any_write(case):
    with api_history() as (lab, database, clock, operations):
        operation_name = "noop" if case.startswith("noop_") else "rejected" if case.startswith("rejected_") else "seat"
        if case.startswith("request_"):
            mutate = lambda operation: corrupt_request(operation, case.removeprefix("request_"))
        else:
            mutate = lambda operation: corrupt_response(operation, case)
        change_operation(lab, database, operations[operation_name], mutate)
        corrupted = database.read_bytes()
        with pytest.raises(StoreValidationError):
            ReceptionStore(database, clock=clock)
        assert database.read_bytes() == corrupted
