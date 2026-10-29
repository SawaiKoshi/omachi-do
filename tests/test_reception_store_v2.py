"""v1/v2 runtime compatibility using only synthetic temporary databases."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sqlite3
from threading import Barrier
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

import reception_store
from reception_migration.migration import copy_database, migrate
from reception_migration.safety import temporary_workspace
from reception_migration.schema import TRIGGERS
from reception_migration.synthetic import BASE, create_v1
from reception_schema import STATE_FIELDS, validate_history, validate_schema
from reception_store import ReceptionStore, StateConflict, StoreValidationError
from staff_operation import canonical_json, command_request, receipt_response


JST = ZoneInfo("Asia/Tokyo")


class Clock:
    def __init__(self, value=None):
        self.value = value or datetime(2026, 10, 24, 12, tzinfo=JST)

    def __call__(self):
        return self.value


@pytest.fixture
def pair():
    with temporary_workspace() as lab:
        v1, _ = create_v1(lab, "v1.sqlite3", populated=False)
        v2 = copy_database(lab, v1, "v2.sqlite3")
        migrate(lab, v2, expected_sha256=sha256(v2.read_bytes()).hexdigest())
        yield lab, {1: v1, 2: v2}


def frozen_v1_store():
    path = Path(__file__).parent / "fixtures/reception_store_v1_f470ddf.py"
    spec = importlib.util.spec_from_file_location("frozen_reception_store_v1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prediction(**kwargs):
    return dict(status="available", minutes=12.25, model_version="synthetic-predictor",
                predicted_at=kwargs["received_at"].isoformat(), warnings=["synthetic only"])


def saved_json(lab, database):
    with lab.connection(database) as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY reception_id")]
            for table in ("reception_snapshots", "predictions", "idempotency_keys")
        }


def exercise(store, clock):
    receipts = [store.register(adults, 0, str(UUID(int=200 + index)), predictor=prediction)[0]
                for index, adults in enumerate((4, 2, 1))]
    answers = list(receipts)
    for target in ("called", "absent", "waiting", "called"):
        clock.value += timedelta(minutes=1)
        answers.append(store.transition(receipts[0]["id"], target))
        answers.append(store.transition(receipts[0]["id"], target))
    answers.append(store.transition(receipts[1]["id"], "seated"))
    answers.append(store.transition(receipts[2]["id"], "cancelled"))
    clock.value += timedelta(hours=13, seconds=30, microseconds=123456)
    answers.append(store.transition(receipts[0]["id"], "seated"))
    answers.append(store.register(4, 0, str(UUID(int=200)), predictor=prediction)[0])
    answers.append(store.get_queue())
    return receipts, answers


def test_same_operations_match_frozen_v1_current_v1_and_v2(pair, monkeypatch):
    lab, paths = pair
    reference = copy_database(lab, paths[1], "reference.sqlite3")
    old = frozen_v1_store()
    results = []
    for module, path in ((old, reference), (reception_store, paths[1]), (reception_store, paths[2])):
        ids = iter(UUID(int=index) for index in (101, 102, 103))
        monkeypatch.setattr(module, "uuid4", lambda: next(ids))
        clock = Clock()
        store = module.ReceptionStore(path, clock=clock)
        receipts, answers = exercise(store, clock)
        results.append((answers, saved_json(lab, path)))
        assert receipts[0]["received_at"][:10] != store.get(receipts[0]["id"])["seated_at"][:10]
        if module is reception_store:
            # Reopening must verify the complete chain, including exact audit projections.
            assert module.ReceptionStore(path, clock=clock).get_queue() == []
    assert results[0] == results[1] == results[2]


def test_existing_migrated_rows_are_unchanged_by_opening_and_unrelated_operations():
    with temporary_workspace() as lab:
        database, ids = create_v1(lab)
        with lab.connection(database) as conn:
            before_rows = [tuple(row) for row in conn.execute("SELECT * FROM receptions ORDER BY reception_order")]
            old_events = [tuple(row) for row in conn.execute("SELECT * FROM reception_events ORDER BY event_id")]
        original_json = saved_json(lab, database)
        migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        migrated = database.read_bytes()
        store = ReceptionStore(database, clock=Clock(BASE + timedelta(hours=1)), expected_schema_version=2)
        assert database.read_bytes() == migrated
        receipt, _ = store.register(2, 0, str(uuid4()), predictor=prediction)
        store.transition(receipt["id"], "seated")
        with lab.connection(database) as conn:
            for original in before_rows:
                row = conn.execute("SELECT * FROM receptions WHERE id=?", (original[1],)).fetchone()
                assert tuple(row[:-1]) == original and row[-1] == 0
            for event in old_events:
                row = conn.execute("SELECT * FROM reception_events WHERE event_id=?", (event[0],)).fetchone()
                assert tuple(row[:8]) == event
            validate_history(conn, 2)
        current_json = saved_json(lab, database)
        for table, rows in original_json.items():
            assert all(row in current_json[table] for row in rows)


def test_v2_audit_states_revisions_and_initial_receipt_are_consistent(pair):
    lab, paths = pair
    clock = Clock()
    store = ReceptionStore(paths[2], clock=clock)
    key = str(uuid4())
    row, _ = store.register(2, 0, key, predictor=prediction)
    initial = saved_json(lab, paths[2])
    for revision, target in enumerate(("called", "absent", "waiting", "called", "seated"), 1):
        clock.value += timedelta(seconds=30)
        updated = store.transition(row["id"], target)
        before_noop = paths[2].read_bytes()
        assert store.transition(row["id"], target) == updated
        assert paths[2].read_bytes() == before_noop
        with lab.connection(paths[2]) as conn:
            assert conn.execute("SELECT revision FROM receptions WHERE id=?", (row["id"],)).fetchone()[0] == revision
            event = store.get_events(row["id"])[-1]
            before, after = json.loads(event["before_state_json"]), json.loads(event["after_state_json"])
            assert set(before) == set(after) == set(STATE_FIELDS)
            assert before["revision"] == revision - 1 and after["revision"] == revision
            assert after["status"] == target and event["audit_kind"] == "staff_legacy_v2"
            assert event["operation_id"] is None
            validate_history(conn, 2)
        assert saved_json(lab, paths[2]) == initial
    assert store.register(2, 0, key) == (row, False)
    with lab.connection(paths[2]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM staff_operations").fetchone()[0] == 0
        for table in ("data_quality_issues", "data_quality_reviews", "data_quality_assessments"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("version", [1, 2])
def test_concurrent_registration_and_same_state_retry_remain_atomic(pair, version):
    lab, paths = pair
    clock = Clock()
    stores = [ReceptionStore(paths[version], clock=clock) for _ in range(6)]
    barrier = Barrier(len(stores))
    def register(store):
        barrier.wait(timeout=5)
        return store.register(2, 0, str(uuid4()))[0]
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        receipts = list(pool.map(register, stores))
    assert sorted(row["snapshot"]["waiting_groups"] for row in receipts) == list(range(6))
    assert sorted(row["reception_order"] for row in receipts) == list(range(1, 7))
    barrier = Barrier(len(stores))
    def seat(store):
        barrier.wait(timeout=5)
        return store.transition(receipts[0]["id"], "seated")
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        outcomes = list(pool.map(seat, stores))
    assert all(result == outcomes[0] for result in outcomes)
    assert len(stores[0].get_events(receipts[0]["id"])) == 2
    with lab.connection(paths[version]) as conn:
        validate_history(conn, version)
        if version == 2:
            assert conn.execute("SELECT revision FROM receptions WHERE id=?", (receipts[0]["id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("version", [1, 2])
def test_same_registration_key_from_independent_connections_creates_once(pair, version):
    lab, paths = pair
    stores = [ReceptionStore(paths[version], clock=Clock()) for _ in range(5)]
    barrier, key = Barrier(len(stores)), str(uuid4())
    def register(store):
        barrier.wait(timeout=5)
        return store.register(2, 0, key)
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        receipts = list(pool.map(register, stores))
    assert sum(created for row, created in receipts) == 1
    assert all(row == receipts[0][0] for row, created in receipts)
    with lab.connection(paths[version]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM receptions").fetchone()[0] == 1
        validate_history(conn, version)


@pytest.mark.parametrize("version", [1, 2])
def test_competing_terminal_operations_only_commit_one_result(pair, version):
    lab, paths = pair
    stores = [ReceptionStore(paths[version], clock=Clock()) for _ in range(2)]
    row, _ = stores[0].register(2, 0, str(uuid4()))
    barrier = Barrier(2)
    def update(item):
        store, target = item
        barrier.wait(timeout=5)
        try:
            return store.transition(row["id"], target)
        except StateConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, zip(stores, ("seated", "cancelled"))))
    assert sum(result is not None for result in outcomes) == 1
    assert len(stores[0].get_events(row["id"])) == 2
    with lab.connection(paths[version]) as conn:
        validate_history(conn, version)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("operation", ["register", "transition", "before_commit"])
def test_write_failure_rolls_back_every_row_clock_sequence_and_revision(pair, monkeypatch, version, operation):
    lab, paths = pair
    store = ReceptionStore(paths[version], clock=Clock())
    key = str(uuid4())
    row = store.register(2, 0, str(uuid4()))[0] if operation == "transition" else None
    original = paths[version].read_bytes()
    append = store._append_event
    connection = store._connection
    if operation == "before_commit":
        @contextmanager
        def failed_commit(*, write=False):
            with connection(write=write) as conn:
                yield conn
                if write:
                    raise RuntimeError("injected before commit")
        monkeypatch.setattr(store, "_connection", failed_commit)
    else:
        def failed_append(*args, **kwargs):
            append(*args, **kwargs)
            raise RuntimeError("injected after event")
        monkeypatch.setattr(store, "_append_event", failed_append)
    with pytest.raises(RuntimeError, match="injected"):
        store.transition(row["id"], "seated") if row else store.register(2, 0, key)
    assert paths[version].read_bytes() == original
    monkeypatch.setattr(store, "_connection", connection)
    monkeypatch.setattr(store, "_append_event", append)
    result = store.transition(row["id"], "seated") if row else store.register(2, 0, key)[0]
    if row is None:
        assert result["reception_order"] == 1
    with lab.connection(paths[version]) as conn:
        validate_history(conn, version)


@pytest.mark.parametrize("target", ["called", "absent", "waiting", "seated", "cancelled"])
def test_revision_required_blocks_every_legacy_staff_update_even_noops(pair, target):
    lab, paths = pair
    store = ReceptionStore(paths[2], clock=Clock())
    row, _ = store.register(2, 0, str(uuid4()))
    with lab.connection(paths[2]) as conn:
        conn.execute("UPDATE store_metadata SET staff_write_mode='revision_required'")
    original = paths[2].read_bytes()
    with pytest.raises(StateConflict, match="revision"):
        store.transition(row["id"], target)
    assert paths[2].read_bytes() == original
    reopened = ReceptionStore(paths[2], clock=Clock())
    assert reopened.get_queue()[0]["id"] == row["id"]
    assert reopened.register(1, 0, str(uuid4()))[1] is True
    with lab.connection(paths[2]) as conn:
        assert conn.execute("SELECT seat_corrections_enabled FROM store_metadata").fetchone()[0] == 0


def test_frozen_v1_code_and_explicit_version_mismatch_reject_before_dml(pair, monkeypatch):
    _, paths = pair
    old = frozen_v1_store()
    real_connect = sqlite3.connect
    statements = []
    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    for factory, path, error in (
        (old.ReceptionStore, paths[2], old.StoreValidationError),
        (lambda p: ReceptionStore(p, expected_schema_version=1), paths[2], StoreValidationError),
        (lambda p: ReceptionStore(p, expected_schema_version=2), paths[1], StoreValidationError),
    ):
        statements.clear()
        original = path.read_bytes()
        with pytest.raises(error):
            factory(path)
        assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")) for sql in statements)
        assert path.read_bytes() == original


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("damage", ["version", "index", "column"])
def test_unknown_schema_fails_before_any_business_write(pair, version, damage):
    lab, paths = pair
    database = paths[version]
    store = ReceptionStore(database, clock=Clock())
    with lab.connection(database) as conn:
        if damage == "version":
            conn.execute("PRAGMA user_version=99")
        elif damage == "index":
            conn.execute("DROP INDEX queue_status_order")
        else:
            conn.execute("ALTER TABLE receptions ADD COLUMN unexpected TEXT")
    original = database.read_bytes()
    for action in (lambda: ReceptionStore(database), lambda: store.register(2, 0, str(uuid4()))):
        with pytest.raises(StoreValidationError):
            action()
        assert database.read_bytes() == original


def test_existing_store_does_not_silently_follow_an_offline_schema_upgrade(pair):
    lab, paths = pair
    store = ReceptionStore(paths[1], clock=Clock())
    migrate(lab, paths[1], expected_sha256=sha256(paths[1].read_bytes()).hexdigest())
    original = paths[1].read_bytes()
    with pytest.raises(StoreValidationError):
        store.register(2, 0, str(uuid4()))
    assert paths[1].read_bytes() == original


@pytest.mark.parametrize("version", [0, 3, True])
def test_invalid_expected_version_does_not_create_database(tmp_path, version):
    database = tmp_path / "missing" / "database.sqlite3"
    with pytest.raises(StoreValidationError):
        ReceptionStore(database, expected_schema_version=version)
    assert not database.parent.exists()


def test_v2_is_never_created_or_migrated_automatically(tmp_path):
    database = tmp_path / "missing.sqlite3"
    with pytest.raises(StoreValidationError):
        ReceptionStore(database, expected_schema_version=2)
    assert not database.exists()
    assert ReceptionStore(database).schema_version == 1


def test_existing_truncated_file_is_never_silently_reinitialized(tmp_path):
    database = tmp_path / "truncated.sqlite3"
    database.touch()
    with pytest.raises(StoreValidationError):
        ReceptionStore(database)
    assert database.read_bytes() == b""


def test_missing_audit_event_rejects_legacy_noop_without_touching_clock(pair):
    lab, paths = pair
    store = ReceptionStore(paths[2], clock=Clock())
    receipt, _ = store.register(2, 0, str(uuid4()))
    with lab.connection(paths[2]) as conn:
        # A direct SQL update outside the store omitted its audit event.
        conn.execute("UPDATE receptions SET revision=revision+1 WHERE id=?", (receipt["id"],))
    original = paths[2].read_bytes()
    with pytest.raises(StoreValidationError):
        store.transition(receipt["id"], "waiting")
    with pytest.raises(StoreValidationError):
        ReceptionStore(paths[2])
    assert paths[2].read_bytes() == original


@pytest.mark.parametrize("field", ["before_state_json", "after_state_json"])
def test_semantically_corrupt_audit_is_rejected_even_if_schema_is_restored(pair, field):
    lab, paths = pair
    store = ReceptionStore(paths[2], clock=Clock())
    receipt, _ = store.register(2, 0, str(uuid4()))
    store.transition(receipt["id"], "called")
    trigger = next(sql for sql in TRIGGERS if sql.split()[2] == "reception_events_no_update")
    with lab.connection(paths[2]) as conn:
        # Test-only corruption in an isolated fixture, followed by the exact DDL.
        conn.execute("DROP TRIGGER reception_events_no_update")
        conn.execute(f"UPDATE reception_events SET {field}='{{}}' WHERE event_type='called'")
        conn.execute(trigger)
    original = paths[2].read_bytes()
    with pytest.raises(StoreValidationError):
        ReceptionStore(paths[2])
    assert paths[2].read_bytes() == original


def test_write_epoch_change_invalidates_the_old_store_until_recreated(pair):
    lab, paths = pair
    store = ReceptionStore(paths[2], clock=Clock())
    receipt, _ = store.register(2, 0, str(uuid4()))
    with lab.connection(paths[2]) as conn:
        conn.execute("UPDATE store_metadata SET write_epoch=?", (str(uuid4()),))
    original = paths[2].read_bytes()
    for action in (lambda: store.transition(receipt["id"], "called"), store.initialize):
        with pytest.raises(StoreValidationError):
            action()
        assert paths[2].read_bytes() == original
    reopened = ReceptionStore(paths[2], clock=Clock())
    assert reopened.transition(receipt["id"], "called")["status"] == "called"


def test_same_version_database_replacement_does_not_rebind_an_existing_store(pair):
    lab, paths = pair
    store = ReceptionStore(paths[2], clock=Clock())
    other, _ = create_v1(lab, "other.sqlite3", populated=False)
    migrate(lab, other, expected_sha256=sha256(other.read_bytes()).hexdigest())
    paths[2].write_bytes(other.read_bytes())
    original = paths[2].read_bytes()
    with pytest.raises(StoreValidationError):
        store.register(2, 0, str(uuid4()))
    assert paths[2].read_bytes() == original
    assert ReceptionStore(paths[2], clock=Clock()).get_queue() == []


@pytest.mark.parametrize("fault", [None, "action", "expected_revision", "resulting_revision", "outcome", "request_sha256", "orphan"])
def test_staff_operation_structure_must_agree_with_its_audit_event(pair, fault):
    lab, paths = pair
    store = ReceptionStore(paths[2], clock=Clock())
    receipt, _ = store.register(2, 0, str(uuid4()))
    store.transition(receipt["id"], "called")
    trigger = next(sql for sql in TRIGGERS if sql.split()[2] == "reception_events_no_update")
    with lab.connection(paths[2]) as conn:
        epoch = conn.execute("SELECT write_epoch FROM store_metadata").fetchone()[0]
        operation = dict(operation_id=str(uuid4()), request_schema_version=1,
                         requested_reception_id=receipt["id"], resolved_reception_id=receipt["id"],
                         action="call", write_epoch=epoch, expected_revision=0,
                         principal_kind="shared_staff_token", principal_id="synthetic-principal",
                         request_json="{}", request_sha256=sha256(b"{}").hexdigest(),
                         outcome="state_changed", response_status=200, response_json="{}",
                         resulting_revision=1, recorded_at=Clock()().isoformat())
        operation["request_json"] = canonical_json(command_request(receipt["id"], "call", 0, epoch))
        operation["request_sha256"] = sha256(operation["request_json"].encode()).hexdigest()
        operation["response_json"] = canonical_json(dict(
            operation_id=operation["operation_id"], reception_id=receipt["id"], action="call",
            outcome="state_changed", response_status=200, recorded_at=operation["recorded_at"],
            resulting_revision=1, reception=receipt_response(store.get(receipt["id"])), error=None,
        ))
        if fault == "action":
            operation["action"] = "seat"
        elif fault == "expected_revision":
            operation["expected_revision"] = 4
        elif fault == "resulting_revision":
            operation["resulting_revision"] = 5
        elif fault == "outcome":
            operation.update(outcome="rejected", response_status=409)
        elif fault == "request_sha256":
            operation["request_sha256"] = "0" * 64
        conn.execute("INSERT INTO staff_operations(" + ",".join(operation) + ") VALUES(" +
                     ",".join("?" for _ in operation) + ")", tuple(operation.values()))
        if fault != "orphan":
            conn.execute("DROP TRIGGER reception_events_no_update")
            conn.execute("UPDATE reception_events SET audit_kind='staff_v2',operation_id=? WHERE event_type='called'",
                         (operation["operation_id"],))
            conn.execute(trigger)
    original = paths[2].read_bytes()
    if fault is None:
        assert ReceptionStore(paths[2], clock=Clock()).get(receipt["id"])["status"] == "called"
    else:
        with pytest.raises(StoreValidationError):
            ReceptionStore(paths[2], clock=Clock())
    assert paths[2].read_bytes() == original
