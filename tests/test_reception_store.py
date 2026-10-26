"""Synthetic reception records only; no real store data or Excel is needed."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
from threading import Barrier, Event
from uuid import uuid4

import pytest

from reception_store import (
    IdempotencyConflict, JST, ReceptionNotFound, ReceptionStore, SCHEMA_VERSION,
    StateConflict, StoreValidationError,
)


class Clock:
    def __init__(self, value=None):
        self.value = value or datetime(2026, 10, 24, 12, tzinfo=JST)

    def __call__(self):
        return self.value


@pytest.fixture
def store(tmp_path):
    return ReceptionStore(tmp_path / "receptions.sqlite3", clock=Clock())


def register(store, adults=2, children=0, **kwargs):
    return store.register(adults, children, str(uuid4()), **kwargs)[0]


def prediction_response(**updates):
    return {"status": "available", "minutes": 12.25, "model_version": "synthetic-v1",
            "predicted_at": "2026-10-24T12:00:00+09:00", "warnings": [], **updates}


def test_registration_separates_identifiers_and_preserves_intake_information(store):
    record = register(store, 2, 1)
    assert record["display_number"] == "20261024-001"
    assert record["id"] != record["display_number"]
    assert record["reception_order"] == 1
    assert (record["adults"], record["children"], record["total_people"]) == (2, 1, 3)
    assert record["status"] == "waiting"
    assert record["received_at"] == record["recorded_at"] == "2026-10-24T12:00:00+09:00"
    assert record["source"] == "reception_api"
    assert record["called_at"] is record["seated_at"] is record["cancelled_at"] is None
    assert record["actual_wait_minutes"] is None
    assert store.get(record["id"]) == record
    assert store.get("unknown") is None


def test_children_only_party_matches_existing_people_validation(store):
    record = register(store, 0, 1)
    assert (record["adults"], record["children"], record["total_people"]) == (0, 1, 1)


@pytest.mark.parametrize("adults,children", [
    (0, 0), (-1, 1), (1, -1), (100, 1), (101, 0),
    (True, 0), (1, False), ("2", 0), (2, "0"), (1.0, 0), (1, 0.0), (None, 0),
])
def test_invalid_people_never_create_a_reception_or_consume_a_number(store, adults, children):
    with pytest.raises(StoreValidationError):
        store.register(adults, children, str(uuid4()))
    assert store.get_queue() == []
    assert register(store)["display_number"].endswith("001")


@pytest.mark.parametrize("key", [None, "", "duplicate", 42])
def test_invalid_idempotency_key_is_rejected(store, key):
    with pytest.raises(StoreValidationError):
        store.register(1, 0, key)
    assert store.get_queue() == []


def test_same_key_replays_original_response_without_predicting_again(store):
    calls = []

    def predict(**kwargs):
        calls.append(kwargs)
        return prediction_response()

    key = str(uuid4())
    first, created = store.register(2, 1, key, predictor=predict)
    assert created is True
    store.transition(first["id"], "seated")
    repeated, created = store.register(2, 1, key.upper(), predictor=predict)
    assert repeated == first
    assert created is False
    assert len(calls) == 1
    assert len(store.get_events(first["id"])) == 2
    with pytest.raises(IdempotencyConflict):
        store.register(3, 0, key)
    assert register(store)["reception_order"] == 2


def test_independent_connections_serialize_numbers_and_queue_snapshots(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    clock = Clock()
    stores = [ReceptionStore(path, clock=clock) for _ in range(12)]
    barrier = Barrier(len(stores))

    def intake(one_store):
        barrier.wait(timeout=5)
        return register(one_store)

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        records = list(pool.map(intake, stores))
    ordered = sorted(records, key=lambda record: record["reception_order"])
    assert [row["reception_order"] for row in ordered] == list(range(1, 13))
    assert len({row["id"] for row in records}) == 12
    assert [row["display_number"] for row in ordered] == [f"20261024-{i:03d}" for i in range(1, 13)]
    assert [row["snapshot"]["waiting_groups"] for row in ordered] == list(range(12))
    assert [row["snapshot"]["ahead_groups"] for row in ordered] == list(range(12))


def test_simultaneous_same_key_creates_one_record_and_runs_predictor_once(tmp_path):
    path = tmp_path / "same-key.sqlite3"
    stores = [ReceptionStore(path, clock=Clock()) for _ in range(6)]
    barrier = Barrier(len(stores))
    key = str(uuid4())
    calls = []

    def predict(**kwargs):
        calls.append(kwargs)
        return prediction_response(minutes=7)

    def intake(one_store):
        barrier.wait(timeout=5)
        return one_store.register(2, 0, key, predictor=predict)

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = list(pool.map(intake, stores))
    assert sum(created for _, created in results) == 1
    assert len({row["id"] for row, _ in results}) == 1
    assert len(calls) == len(stores[0].get_queue()) == 1


def test_clock_is_read_after_obtaining_the_intake_write_lock(tmp_path):
    path = tmp_path / "lock-time.sqlite3"
    store = ReceptionStore(path, clock=Clock())
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    started = Event()
    clock_read = Event()

    def clock():
        clock_read.set()
        return datetime(2026, 10, 25, tzinfo=JST)

    store.clock = clock

    def intake():
        started.set()
        return register(store)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(intake)
            assert started.wait(2)
            assert not clock_read.wait(0.05)
            blocker.commit()
            assert future.result(timeout=5)["display_number"] == "20261025-001"
    finally:
        blocker.close()


def test_queue_bands_include_called_and_absent_but_exclude_terminal_records(store):
    first = register(store, 1, 0)
    second = register(store, 2, 2)
    third = register(store, 3, 2)
    store.transition(first["id"], "called")
    store.transition(second["id"], "called")
    store.transition(second["id"], "absent")
    snapshot = store.snapshot(3)
    assert snapshot == {
        "captured_at": "2026-10-24T12:00:00+09:00", "source": "reception_database",
        "waiting_groups": 3, "ahead_groups": 3, "ahead_same_size_groups": 1,
        "party_size_groups": {"1-2": 1, "3-4": 1, "5+": 1},
    }
    store.transition(third["id"], "cancelled")
    store.transition(first["id"], "seated")
    assert [row["id"] for row in store.get_queue()] == [second["id"]]
    assert store.snapshot(3)["party_size_groups"] == {"1-2": 0, "3-4": 1, "5+": 0}


def test_registered_snapshot_is_immutable_after_seating_cancellation_and_new_intake(store):
    first = register(store, 2)
    second = register(store, 3)
    third = register(store, 4)
    saved_snapshot = third["snapshot"]
    assert saved_snapshot["waiting_groups"] == 2
    assert saved_snapshot["ahead_same_size_groups"] == 1
    store.transition(first["id"], "seated")
    store.transition(second["id"], "cancelled")
    register(store, 5)
    assert store.get(third["id"])["snapshot"] == saved_snapshot


def test_later_smaller_group_can_be_seated_first_and_actual_wait_is_unrounded(store):
    first = register(store, 4)
    store.clock.value += timedelta(minutes=1)
    later = register(store, 2)
    store.clock.value += timedelta(minutes=2, seconds=30, microseconds=123456)
    seated_later = store.transition(later["id"], "seated")
    assert seated_later["actual_wait_minutes"] == pytest.approx(2.5 + 0.123456 / 60)
    assert [row["id"] for row in store.get_queue()] == [first["id"]]
    store.clock.value += timedelta(minutes=4)
    seated_first = store.transition(first["id"], "seated")
    assert seated_first["actual_wait_minutes"] > seated_later["actual_wait_minutes"]
    assert first["reception_order"] < later["reception_order"]


def test_calling_and_absence_do_not_create_seating_time_or_actual_wait(store):
    first = register(store)
    for target in ("called", "absent", "waiting", "called"):
        store.clock.value += timedelta(minutes=1)
        row = store.transition(first["id"], target)
        assert row["seated_at"] is row["actual_wait_minutes"] is None
    assert row["called_at"] == "2026-10-24T12:04:00+09:00"
    store.clock.value += timedelta(minutes=2)
    seated = store.transition(first["id"], "seated")
    assert seated["actual_wait_minutes"] == 6
    assert seated["seated_at"] != seated["called_at"]
    assert [row["event_type"] for row in store.get_events(first["id"])] == [
        "registered", "called", "absent", "waiting", "called", "seated",
    ]
    assert store.get_events(first["id"])[-1]["source"] == "staff_operation"


@pytest.mark.parametrize("terminal", ["seated", "cancelled"])
def test_terminal_states_cannot_be_reopened_and_repeated_operation_has_no_event(store, terminal):
    row = register(store)
    final = store.transition(row["id"], terminal)
    assert store.transition(row["id"], terminal) == final
    assert len(store.get_events(row["id"])) == 2
    for target in {"waiting", "called", "absent", "seated", "cancelled"} - {terminal}:
        with pytest.raises(StateConflict):
            store.transition(row["id"], target)
    if terminal == "cancelled":
        assert final["actual_wait_minutes"] is final["seated_at"] is None
        assert final["cancelled_at"] is not None
    else:
        assert final["actual_wait_minutes"] == 0
        assert final["cancelled_at"] is None


def test_invalid_state_transitions_and_missing_receptions_are_explicit(store):
    row = register(store)
    for target in ("absent", "invented"):
        with pytest.raises(StateConflict):
            store.transition(row["id"], target)
    with pytest.raises(ReceptionNotFound):
        store.transition("missing", "seated")
    with pytest.raises(ReceptionNotFound):
        store.get_events("missing")
    assert len(store.get_events(row["id"])) == 1


def test_numbers_reset_by_jst_calendar_day_and_unresolved_groups_remain_active(store):
    store.clock.value = datetime(2026, 10, 24, 14, 59, tzinfo=timezone.utc)
    first = register(store)
    store.clock.value += timedelta(minutes=1)
    second = register(store)
    assert first["display_number"] == "20261024-001"
    assert second["display_number"] == "20261025-001"
    assert second["snapshot"]["waiting_groups"] == 1
    assert len(store.get_queue()) == 2
    store.clock.value += timedelta(minutes=1)
    assert store.transition(first["id"], "seated")["actual_wait_minutes"] == 2


@pytest.mark.parametrize("bad_time", [datetime(2026, 10, 24, 12), "2026-10-24", None])
def test_non_aware_or_non_datetime_clock_is_rejected_without_partial_records(store, bad_time):
    store.clock = lambda: bad_time
    with pytest.raises(StoreValidationError):
        register(store)
    assert store.get_queue() == []


def test_time_reversal_is_rejected_for_intake_and_staff_operations_without_partial_writes(store):
    first = register(store)
    store.clock.value -= timedelta(microseconds=1)
    with pytest.raises(StoreValidationError):
        store.transition(first["id"], "seated")
    with pytest.raises(StoreValidationError):
        register(store)
    assert store.get(first["id"])["status"] == "waiting"
    assert len(store.get_events(first["id"])) == 1
    store.clock.value += timedelta(microseconds=1)
    assert register(store)["display_number"].endswith("002")


def test_model_absence_or_failure_never_stops_intake_and_keeps_no_exception_details(store):
    absent = register(store)
    assert absent["prediction"]["status"] == "model_unavailable"

    def fail(**kwargs):
        raise RuntimeError("private raw data must not be saved")

    failed = register(store, predictor=fail)
    assert failed["prediction"]["status"] == "prediction_failed"
    assert failed["prediction"]["minutes"] is failed["prediction"]["model_version"] is None
    assert "private raw data" not in str(failed)
    assert len(store.get_queue()) == 2


@pytest.mark.parametrize("result", [
    None, [], {}, {"status": "available", "minutes": 1, "model_version": "synthetic-v1"},
    prediction_response(minutes=float("nan")), prediction_response(minutes=-1),
    prediction_response(minutes=True), prediction_response(minutes=None),
    prediction_response(model_version=None), prediction_response(model_version=""),
    prediction_response(model_version=1), prediction_response(warnings="warning"),
    prediction_response(warnings=[1]), prediction_response(predicted_at=None),
    prediction_response(predicted_at="2026-10-24T12:00:00"),
    prediction_response(predicted_at="not-a-date"),
    prediction_response(status="made_up"), prediction_response(status=["available"]),
    prediction_response(status="model_unavailable", minutes=1),
    prediction_response(unserializable=object()),
])
def test_invalid_prediction_output_is_unavailable_but_intake_survives(store, result):
    row = register(store, predictor=lambda **kwargs: result)
    assert row["prediction"]["status"] == "prediction_failed"
    assert row["status"] == "waiting"


def test_prediction_uses_same_transaction_queue_snapshot_and_preserves_version(store):
    register(store, 1)
    received = []
    prediction = {
        "status": "available", "minutes": 13.1234, "model_version": "synthetic-v1",
        "predicted_at": store.clock.value.isoformat(), "warnings": ["検証用"],
    }

    def predict(**kwargs):
        received.append(kwargs)
        return prediction

    row = register(store, 2, 1, predictor=predict)
    assert received == [{"adults": 2, "children": 1, "received_at": store.clock.value,
                         "waiting_groups": 1}]
    assert row["prediction"] == prediction
    prediction["minutes"] = 999
    assert store.get(row["id"])["prediction"]["minutes"] == 13.1234
    assert row["actual_wait_minutes"] is None


def test_snapshot_is_read_only_and_does_not_allocate_numbers(store):
    for _ in range(3):
        assert store.snapshot(2)["waiting_groups"] == 0
    assert store.get_queue() == []
    assert register(store)["display_number"].endswith("001")


@pytest.mark.parametrize("total", [0, -1, 101, True, "2", 1.5])
def test_invalid_hypothetical_party_size_is_rejected(store, total):
    with pytest.raises(StoreValidationError):
        store.snapshot(total)


def test_database_reopens_with_records_events_and_reproducibility_metadata(store):
    row = register(store)
    reopened = ReceptionStore(store.db_path, clock=store.clock)
    assert reopened.get(row["id"]) == row
    assert reopened.get_events(row["id"])[0]["occurred_at"] == row["received_at"]
    with closing(sqlite3.connect(store.db_path, isolation_level=None)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT predicted_at, source FROM predictions").fetchone() == (
            row["received_at"], "local_prediction_service",
        )
    assert register(reopened)["display_number"].endswith("002")


def test_unknown_database_or_schema_version_is_rejected_without_migration(tmp_path):
    wrong = tmp_path / "foreign.sqlite3"
    with closing(sqlite3.connect(wrong, isolation_level=None)) as conn:
        conn.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(StoreValidationError):
        ReceptionStore(wrong)
    path = tmp_path / "wrong-version.sqlite3"
    ReceptionStore(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(StoreValidationError):
        ReceptionStore(path)


def test_database_constraints_reject_invalid_people_and_terminal_dates(store):
    row = register(store)
    with closing(sqlite3.connect(store.db_path, isolation_level=None)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE receptions SET adults = 0 WHERE id = ?", (row["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE receptions SET status = 'seated' WHERE id = ?", (row["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE receptions SET called_at = '2026-10-24T11:00:00+09:00' WHERE id = ?",
                         (row["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE receptions SET called_at = 'not-a-date' WHERE id = ?", (row["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE reception_events SET recorded_at = 'not-a-date' WHERE reception_id = ?",
                         (row["id"],))
