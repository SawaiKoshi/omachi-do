"""Protected staff writes; every database is synthetic and lives under /tmp."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
from threading import Barrier
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_schema import validate_history, validate_schema
from reception_store import ReceptionNotFound, ReceptionStore, StateConflict, StoreValidationError


ACTOR = {"principal_kind": "shared_staff_token", "principal_id": "shared-staff"}
TARGETS = {"call": "called", "absent": "absent", "return": "waiting",
           "seat": "seated", "cancel": "cancelled"}


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 29, 23, 59, 30, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


def prediction(**kwargs):
    return {"status": "available", "minutes": 12.125, "model_version": "synthetic-only",
            "predicted_at": kwargs["received_at"].isoformat(), "warnings": []}


@contextmanager
def synthetic_store(*, version=2, mode="revision_required"):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, "staff.sqlite3", populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
            if mode != "legacy":
                with lab.connection(database) as conn:
                    conn.execute("UPDATE store_metadata SET staff_write_mode=? WHERE singleton=1", (mode,))
        clock = Clock()
        store = ReceptionStore(database, clock=clock)
        yield lab, database, store, clock


@pytest.fixture
def protected():
    with synthetic_store() as context:
        yield context


def register(store, *, adults=2):
    key = str(uuid4())
    row, created = store.register(adults, 0, key, predictor=prediction)
    assert created
    return row, key


def request(store, row, action, **overrides):
    current = store.get_staff_reception(row["id"])
    args = dict(operation_id=str(uuid4()), expected_revision=current["revision"],
                write_epoch=current["write_epoch"], **ACTOR)
    args.update(overrides)
    return store.staff_transition(row["id"], action, **args)


def saved_json(lab, database):
    with lab.connection(database, readonly=True) as conn:
        return {table: list(conn.execute(f"SELECT * FROM {table} ORDER BY reception_id"))
                for table in ("reception_snapshots", "predictions", "idempotency_keys")}


def public_receipt(row):
    return {key: value for key, value in row.items() if key not in {"recorded_at", "source"}}


def counts(lab, database):
    with lab.connection(database, readonly=True) as conn:
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("reception_events", "staff_operations"))


def assert_valid(lab, database):
    with lab.connection(database, readonly=True) as conn:
        validate_schema(conn, 2, allow_revision_required=True)
        validate_history(conn, 2)


@pytest.mark.parametrize("action", TARGETS)
def test_all_five_actions_have_revision_operation_and_audit_protection(protected, action):
    lab, database, store, clock = protected
    row, _ = register(store)
    if action in {"absent", "return"}:
        request(store, row, "call")
    if action == "return":
        request(store, row, "absent")
    before = store.get_staff_reception(row["id"])
    clock.value += timedelta(seconds=45, microseconds=123456)
    op_id = str(uuid4())
    result = request(store, row, action, operation_id=op_id)
    assert result["operation_id"] == op_id
    assert result["reception_id"] == row["id"]
    assert result["action"] == action
    assert result["outcome"] == "state_changed"
    assert 200 <= result["response_status"] < 300
    assert result["resulting_revision"] == before["revision"] + 1
    assert result["reception"] == public_receipt(store.get(row["id"]))
    assert result["reception"]["status"] == TARGETS[action]
    assert store.get_staff_reception(row["id"])["revision"] == before["revision"] + 1
    event = store.get_events(row["id"])[-1]
    assert event["audit_kind"] == "staff_v2"
    assert event["operation_id"] == op_id
    assert event["revision_before"] == before["revision"]
    assert event["revision_after"] == before["revision"] + 1
    assert json.loads(event["after_state_json"])["status"] == TARGETS[action]
    with lab.connection(database, readonly=True) as conn:
        operation = conn.execute("SELECT request_json,request_sha256,principal_kind,principal_id "
                                 "FROM staff_operations WHERE operation_id=?", (op_id,)).fetchone()
        assert sha256(operation[0].encode()).hexdigest() == operation[1]
        assert tuple(operation[2:]) == tuple(ACTOR.values())
    assert_valid(lab, database)
    assert public_receipt(ReceptionStore(database, clock=clock).get(row["id"])) == result["reception"]


def test_seating_retry_and_result_lookup_after_lost_response_are_exact_and_write_nothing(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    current = store.get_staff_reception(row["id"])
    args = dict(operation_id=str(uuid4()), expected_revision=0,
                write_epoch=current["write_epoch"], **ACTOR)
    clock.value += timedelta(seconds=75, microseconds=125000)
    first = store.staff_transition(row["id"], "seat", **args)
    assert first["reception"]["actual_wait_minutes"] == 75.125 / 60
    original = database.read_bytes()
    clock.value += timedelta(days=1)
    store = ReceptionStore(database, clock=clock)
    for _ in range(3):
        assert store.staff_transition(row["id"], "seat", **args) == first
        assert store.get_staff_operation(args["operation_id"], **ACTOR) == first
        assert database.read_bytes() == original
    assert counts(lab, database) == (2, 1)
    assert store.get_staff_reception(row["id"])["revision"] == 1
    assert_valid(lab, database)


def test_retry_returns_historical_result_after_another_staff_operation(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    args = dict(operation_id=str(uuid4()), expected_revision=0,
                write_epoch=store.get_staff_reception(row["id"])["write_epoch"], **ACTOR)
    original_call = store.staff_transition(row["id"], "call", **args)
    request(store, row, "absent")
    before = database.read_bytes()
    assert store.staff_transition(row["id"], "call", **args) == original_call
    assert store.get_staff_operation(args["operation_id"], **ACTOR) == original_call
    assert store.get(row["id"])["status"] == "absent"
    assert database.read_bytes() == before
    assert counts(lab, database) == (3, 2)
    assert_valid(lab, database)


@pytest.mark.parametrize("changed", ["reception", "action", "revision", "epoch", "principal_id", "principal_kind"])
def test_operation_id_reuse_with_any_different_request_or_actor_is_rejected_without_writes(protected, changed):
    lab, database, store, clock = protected
    row, _ = register(store)
    other, _ = register(store)
    op_id = str(uuid4())
    args = dict(operation_id=op_id, expected_revision=0,
                write_epoch=store.get_staff_reception(row["id"])["write_epoch"], **ACTOR)
    first = store.staff_transition(row["id"], "call", **args)
    rid, action = row["id"], "call"
    if changed == "reception":
        rid = other["id"]
    elif changed == "action":
        action = "seat"
    elif changed == "revision":
        args["expected_revision"] = 1
    elif changed == "epoch":
        args["write_epoch"] = str(uuid4())
    elif changed == "principal_id":
        args["principal_id"] = "another-staff"
    else:
        args["principal_kind"] = "staff_account"
    before = database.read_bytes()
    with pytest.raises(StateConflict):
        store.staff_transition(rid, action, **args)
    assert database.read_bytes() == before
    assert store.get_staff_operation(op_id, **ACTOR) == first
    assert_valid(lab, database)


@pytest.mark.parametrize("action", TARGETS)
def test_stale_revision_for_every_action_is_persisted_rejection_without_state_or_event_changes(protected, action):
    lab, database, store, clock = protected
    row, _ = register(store)
    request(store, row, "call")
    snapshot = store.get(row["id"])
    events = store.get_events(row["id"])
    op_id = str(uuid4())
    result = request(store, row, action, operation_id=op_id, expected_revision=0)
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert result["error"]
    assert store.get(row["id"]) == snapshot
    assert store.get_events(row["id"]) == events
    assert store.get_staff_reception(row["id"])["revision"] == 1
    assert store.get_staff_operation(op_id, **ACTOR) == result
    before = database.read_bytes()
    assert request(store, row, action, operation_id=op_id, expected_revision=0) == result
    assert database.read_bytes() == before
    assert_valid(lab, database)


def test_noop_requires_current_revision_and_epoch_and_persists_once(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    first = request(store, row, "call")
    events = store.get_events(row["id"])
    op_id = str(uuid4())
    unchanged = request(store, row, "call", operation_id=op_id)
    assert unchanged["outcome"] == "no_change"
    assert unchanged["resulting_revision"] == first["resulting_revision"] == 1
    assert store.get_events(row["id"]) == events
    before = database.read_bytes()
    assert request(store, row, "call", operation_id=op_id) == unchanged
    assert database.read_bytes() == before
    stale = request(store, row, "call", expected_revision=0)
    assert stale["outcome"] == "rejected" and stale["response_status"] == 409
    wrong_epoch = request(store, row, "call", write_epoch=str(uuid4()))
    assert wrong_epoch["outcome"] == "rejected" and wrong_epoch["response_status"] == 409
    assert store.get_events(row["id"]) == events
    assert_valid(lab, database)


@pytest.mark.parametrize("action", TARGETS)
def test_fresh_noop_for_each_action_does_not_rewrite_actual_time_or_revision(protected, action):
    lab, database, store, clock = protected
    row, _ = register(store)
    if action in {"absent", "return"}:
        request(store, row, "call")
    if action == "return":
        request(store, row, "absent")
    first = request(store, row, action)
    events = store.get_events(row["id"])
    clock.value += timedelta(hours=1)
    second = request(store, row, action)
    assert second["outcome"] == "no_change"
    assert second["reception"] == first["reception"]
    assert second["resulting_revision"] == first["resulting_revision"]
    assert store.get_events(row["id"]) == events
    assert_valid(lab, database)


@pytest.mark.parametrize("initial,action", [(None, "absent"), ("seat", "call"),
                                           ("seat", "cancel"), ("cancel", "seat"),
                                           ("cancel", "return")])
def test_invalid_state_transition_is_persisted_without_inventing_event(protected, initial, action):
    lab, database, store, clock = protected
    row, _ = register(store)
    if initial:
        request(store, row, initial)
    current = store.get_staff_reception(row["id"])
    events = store.get_events(row["id"])
    op_id = str(uuid4())
    result = request(store, row, action, operation_id=op_id)
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert result["error"]
    assert store.get_staff_reception(row["id"]) == current
    assert store.get_events(row["id"]) == events
    assert store.get_staff_operation(op_id, **ACTOR) == result
    assert_valid(lab, database)


@pytest.mark.parametrize("action", TARGETS)
def test_wrong_write_epoch_blocks_all_actions_without_mutation(protected, action):
    lab, database, store, clock = protected
    row, _ = register(store)
    if action in {"absent", "return"}:
        request(store, row, "call")
    if action == "return":
        request(store, row, "absent")
    current = store.get_staff_reception(row["id"])
    events = store.get_events(row["id"])
    result = request(store, row, action, write_epoch=str(uuid4()))
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert store.get_staff_reception(row["id"]) == current
    assert store.get_events(row["id"]) == events
    assert_valid(lab, database)


def test_restored_epoch_rejects_unknown_old_commands_but_replays_archived_results(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    old_epoch = store.get_staff_reception(row["id"])["write_epoch"]
    op_id = str(uuid4())
    old_args = dict(operation_id=op_id, expected_revision=0, write_epoch=old_epoch, **ACTOR)
    call = store.staff_transition(row["id"], "call", **old_args)
    with lab.connection(database) as conn:
        conn.execute("UPDATE store_metadata SET write_epoch=? WHERE singleton=1", (str(uuid4()),))
    before = database.read_bytes()
    with pytest.raises(StoreValidationError):
        store.staff_transition(row["id"], "call", **old_args)
    assert database.read_bytes() == before
    reopened = ReceptionStore(database, clock=clock)
    assert reopened.get_staff_reception(row["id"])["write_epoch"] != old_epoch
    assert reopened.staff_transition(row["id"], "call", **old_args) == call
    assert database.read_bytes() == before
    result = request(reopened, row, "seat", write_epoch=old_epoch)
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert reopened.get(row["id"])["status"] == "called"
    request(reopened, row, "seat")
    assert_valid(lab, database)


@pytest.mark.parametrize("target", ["called", "absent", "waiting", "seated", "cancelled"])
def test_required_mode_cannot_be_bypassed_by_old_transition_even_after_protected_write(protected, target):
    lab, database, store, clock = protected
    row, _ = register(store)
    request(store, row, "call")
    before = database.read_bytes()
    with pytest.raises(StateConflict):
        store.transition(row["id"], target)
    assert database.read_bytes() == before
    assert counts(lab, database) == (2, 1)


def test_legacy_mode_allows_new_protected_operations_and_old_writes_share_revisions():
    with synthetic_store(mode="legacy") as (lab, database, store, clock):
        row, _ = register(store)
        request(store, row, "call")
        store.transition(row["id"], "absent")
        assert store.get_staff_reception(row["id"])["revision"] == 2
        stale = request(store, row, "seat", expected_revision=1)
        assert stale["outcome"] == "rejected"
        result = request(store, row, "return")
        assert result["resulting_revision"] == 3
        assert [event["audit_kind"] for event in store.get_events(row["id"])] == [
            "registration_v2", "staff_v2", "staff_legacy_v2", "staff_v2"]
        assert_valid(lab, database)


def test_v1_protected_operations_are_refused_without_automatic_migration():
    with synthetic_store(version=1) as (lab, database, store, clock):
        row, _ = register(store)
        before = database.read_bytes()
        with pytest.raises(StateConflict):
            store.staff_transition(row["id"], "seat", operation_id=str(uuid4()), expected_revision=0,
                                   write_epoch=str(uuid4()), **ACTOR)
        assert database.read_bytes() == before
        store.transition(row["id"], "seated")


@pytest.mark.parametrize("failure_stage", ["before_event", "after_event", "after_ledger", "before_commit"])
def test_exception_at_each_write_stage_rolls_back_state_history_ledger_clock_and_sequence(protected, monkeypatch, failure_stage):
    lab, database, store, clock = protected
    row, _ = register(store)
    before = database.read_bytes()
    append, save, connection = store._append_event, store._save_staff_operation, store._connection
    if failure_stage == "before_commit":
        @contextmanager
        def broken(*, write=False):
            with connection(write=write) as conn:
                yield conn
                if write:
                    raise RuntimeError("injected before commit")
        monkeypatch.setattr(store, "_connection", broken)
    elif failure_stage == "after_ledger":
        def broken(*args, **kwargs):
            save(*args, **kwargs)
            raise RuntimeError("injected after ledger")
        monkeypatch.setattr(store, "_save_staff_operation", broken)
    else:
        def broken(*args, **kwargs):
            if failure_stage == "after_event":
                append(*args, **kwargs)
            raise RuntimeError("injected event stage")
        monkeypatch.setattr(store, "_append_event", broken)
    op_id = str(uuid4())
    with pytest.raises(RuntimeError, match="injected"):
        request(store, row, "seat", operation_id=op_id)
    assert database.read_bytes() == before
    monkeypatch.setattr(store, "_append_event", append)
    monkeypatch.setattr(store, "_save_staff_operation", save)
    monkeypatch.setattr(store, "_connection", connection)
    assert counts(lab, database) == (1, 0)
    result = request(store, row, "seat", operation_id=op_id)
    assert result["outcome"] == "state_changed" and result["resulting_revision"] == 1
    assert_valid(lab, database)


def test_identical_concurrent_requests_commit_one_operation_and_one_seating(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    stores = [ReceptionStore(database, clock=clock) for _ in range(5)]
    args = dict(operation_id=str(uuid4()), expected_revision=0,
                write_epoch=store.get_staff_reception(row["id"])["write_epoch"], **ACTOR)
    barrier = Barrier(len(stores))
    def worker(other):
        barrier.wait(timeout=10)
        return other.staff_transition(row["id"], "seat", **args)
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = list(pool.map(worker, stores))
    assert all(result == results[0] for result in results)
    assert results[0]["outcome"] == "state_changed"
    assert counts(lab, database) == (2, 1)
    assert store.get_staff_reception(row["id"])["revision"] == 1
    assert_valid(lab, database)


def test_competing_different_operations_cannot_both_commit_same_expected_revision(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    stores = [ReceptionStore(database, clock=clock) for _ in range(2)]
    epoch, barrier = store.get_staff_reception(row["id"])["write_epoch"], Barrier(2)
    def worker(item):
        other, action = item
        barrier.wait(timeout=10)
        return other.staff_transition(row["id"], action, operation_id=str(uuid4()),
                                      expected_revision=0, write_epoch=epoch, **ACTOR)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, zip(stores, ("seat", "cancel"))))
    assert sorted(result["outcome"] for result in results) == ["rejected", "state_changed"]
    rejected = next(result for result in results if result["outcome"] == "rejected")
    assert rejected["response_status"] == 409
    assert store.get_staff_reception(row["id"])["revision"] == 1
    assert counts(lab, database) == (2, 2)
    assert_valid(lab, database)


def test_simultaneous_different_requests_with_same_operation_id_have_one_owner(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    stores = [ReceptionStore(database, clock=clock) for _ in range(2)]
    args = dict(operation_id=str(uuid4()), expected_revision=0,
                write_epoch=store.get_staff_reception(row["id"])["write_epoch"], **ACTOR)
    barrier = Barrier(2)
    def worker(item):
        other, action = item
        barrier.wait(timeout=10)
        try:
            return other.staff_transition(row["id"], action, **args)
        except StateConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, zip(stores, ("seat", "cancel"))))
    assert sum(result is not None for result in results) == 1
    assert next(result for result in results if result is not None)["outcome"] == "state_changed"
    assert counts(lab, database) == (2, 1)
    assert_valid(lab, database)


@pytest.mark.parametrize("elapsed", [timedelta(), timedelta(seconds=1, microseconds=500000), timedelta(days=1, seconds=37)])
def test_original_receipt_features_predictions_and_initial_response_are_preserved(protected, elapsed):
    lab, database, store, clock = protected
    row, key = register(store, adults=4)
    original_json = saved_json(lab, database)
    originals = {field: row[field] for field in ("id", "display_number", "reception_order", "received_at",
                                               "adults", "children", "total_people")}
    for action in ("call", "absent", "return", "call"):
        request(store, row, action)
    clock.value += elapsed
    seated = request(store, row, "seat")["reception"]
    assert {field: seated[field] for field in originals} == originals
    assert seated["actual_wait_minutes"] == elapsed.total_seconds() / 60
    assert saved_json(lab, database) == original_json
    assert store.register(4, 0, key) == (row, False)
    assert "revision" not in row and "write_epoch" not in row
    assert_valid(lab, database)


def test_missing_reception_is_archived_as_rejection_and_scoped_result_lookup(protected):
    lab, database, store, clock = protected
    missing, op_id = str(uuid4()), str(uuid4())
    with lab.connection(database, readonly=True) as conn:
        epoch = conn.execute("SELECT write_epoch FROM store_metadata WHERE singleton=1").fetchone()[0]
    args = dict(operation_id=op_id, expected_revision=0, write_epoch=epoch, **ACTOR)
    result = store.staff_transition(missing, "seat", **args)
    assert result["outcome"] == "rejected" and result["response_status"] == 404
    assert result["reception"] is None
    assert result["resulting_revision"] is None
    assert store.get_staff_operation(op_id, **ACTOR) == result
    before = database.read_bytes()
    assert store.staff_transition(missing, "seat", **args) == result
    for actor in ({"principal_kind": "shared_staff_token", "principal_id": "other"},
                  {"principal_kind": "staff_account", "principal_id": "shared-staff"}):
        with pytest.raises(ReceptionNotFound):
            store.get_staff_operation(op_id, **actor)
    with pytest.raises(ReceptionNotFound):
        store.get_staff_operation(str(uuid4()), **ACTOR)
    assert database.read_bytes() == before
    assert counts(lab, database) == (0, 1)
    assert_valid(lab, database)


@pytest.mark.parametrize("overrides", [
    {"operation_id": "not-a-uuid"}, {"operation_id": str(UUID(int=0))},
    {"expected_revision": -1}, {"expected_revision": True}, {"expected_revision": 0.0},
    {"expected_revision": 2 ** 63},
    {"expected_revision": "0"}, {"write_epoch": "bad"}, {"write_epoch": str(UUID(int=0))},
    {"principal_kind": "token"}, {"principal_id": ""}, {"principal_id": "   "},
    {"principal_id": "x" * 129},
])
def test_malformed_operations_do_not_claim_id_change_clock_or_write_history(protected, overrides):
    lab, database, store, clock = protected
    row, _ = register(store)
    before = database.read_bytes()
    with pytest.raises(StoreValidationError):
        request(store, row, "call", **overrides)
    assert database.read_bytes() == before
    assert counts(lab, database) == (1, 0)


@pytest.mark.parametrize("action", ["seated", "seating_correction", "quality_review", "unknown", ""])
def test_unimplemented_or_invalid_actions_never_write(protected, action):
    lab, database, store, clock = protected
    row, _ = register(store)
    before = database.read_bytes()
    with pytest.raises(StoreValidationError):
        request(store, row, action)
    assert database.read_bytes() == before
    assert counts(lab, database) == (1, 0)
