"""Synthetic-only seating correction, ordering, quarantine and atomicity tests."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_store import ReceptionStore, StateConflict, StoreValidationError


ACTOR = {"principal_kind": "shared_staff_token", "principal_id": "shared-staff"}
INTAKE_TABLES = ("reception_snapshots", "predictions", "idempotency_keys", "daily_counters")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 29, 18, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


def prediction(**kwargs):
    return {"status": "available", "minutes": 12.125, "model_version": "synthetic-only",
            "predicted_at": kwargs["received_at"].isoformat(), "warnings": []}


@contextmanager
def synthetic_store(*, version=2, mode="revision_required", enabled=1):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, "corrections.sqlite3", populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
            with lab.connection(database) as conn:
                conn.execute("UPDATE store_metadata SET staff_write_mode=?,seat_corrections_enabled=?",
                             (mode, enabled))
        clock = Clock()
        store = ReceptionStore(database, clock=clock)
        yield lab, database, store, clock


@pytest.fixture
def protected():
    with synthetic_store() as context:
        yield context


def register(store, *, adults=2, children=0):
    key = str(uuid4())
    row, created = store.register(adults, children, key, predictor=prediction)
    assert created
    return row, key


def command_args(store, row, **overrides):
    latest = store.get_staff_reception(row["id"])
    args = dict(operation_id=str(uuid4()), expected_revision=latest["revision"],
                write_epoch=latest["write_epoch"], **ACTOR)
    args.update(overrides)
    return args


def transition(store, row, action, **overrides):
    return store.staff_transition(row["id"], action, **command_args(store, row, **overrides))


def seating_event(store, row):
    return next(event for event in reversed(store.get_events(row["id"]))
                if event["event_type"] == "seated")


def correction_args(store, row, **overrides):
    args = command_args(store, row)
    args.update(target_event_id=seating_event(store, row)["event_id"],
                reason_code="accidental_seating", reason_detail="誤って案内完了を押したため")
    args.update(overrides)
    return args


def correct(store, row, **overrides):
    return store.correct_seating(row["id"], **correction_args(store, row, **overrides))


def rows(lab, database, table):
    with lab.connection(database, readonly=True) as conn:
        cursor = conn.execute(f"SELECT * FROM {table}")
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor]


def intake(lab, database):
    return {table: rows(lab, database, table) for table in INTAKE_TABLES}


def issues(lab, database, rid=None):
    found = rows(lab, database, "data_quality_issues")
    return found if rid is None else [issue for issue in found if issue["reception_id"] == rid]


def reopened(database, clock):
    # Startup must replay correction history and validate every committed ledger.
    return ReceptionStore(database, clock=clock)


@pytest.mark.parametrize("origin", ["waiting", "called", "absent"])
def test_correction_restores_seating_origin_without_rewriting_intake_or_old_event(protected, origin):
    lab, database, store, clock = protected
    receipt, key = register(store, adults=3, children=1)
    if origin in {"called", "absent"}:
        clock.value += timedelta(minutes=1)
        transition(store, receipt, "call")
    if origin == "absent":
        clock.value += timedelta(minutes=1)
        transition(store, receipt, "absent")
    original = store.get(receipt["id"])
    immutable = intake(lab, database)
    clock.value += timedelta(minutes=3)
    seated = transition(store, receipt, "seat")
    target = seating_event(store, receipt)
    old_events = store.get_events(receipt["id"])
    clock.value += timedelta(seconds=1)
    result = correct(store, receipt)
    assert result["outcome"] == "state_changed"
    assert result["action"] == "seating_correction"
    assert result["resulting_revision"] == seated["resulting_revision"] + 1
    restored = store.get(receipt["id"])
    assert restored == original
    assert restored["status"] == origin
    assert restored["called_at"] == original["called_at"]
    assert restored["seated_at"] is None and restored["actual_wait_minutes"] is None
    events = store.get_events(receipt["id"])
    assert events[:-1] == old_events
    audit = events[-1]
    assert audit["event_type"] == "seating_corrected"
    assert audit["from_status"] == "seated" and audit["to_status"] == origin
    assert audit["corrects_event_id"] == target["event_id"]
    assert audit["operation_id"] == result["operation_id"]
    assert audit["reason_code"] == "accidental_seating"
    assert audit["reason_detail"] == "誤って案内完了を押したため"
    assert json.loads(audit["before_state_json"])["status"] == "seated"
    assert json.loads(audit["after_state_json"])["status"] == origin
    assert intake(lab, database) == immutable
    assert store.register(3, 1, key) == (receipt, False)
    holds = issues(lab, database, receipt["id"])
    assert {issue["issue_kind"] for issue in holds} == {"invalidated_seating"}
    assert holds[0]["evidence_event_id"] == target["event_id"]
    assert holds[0]["correction_event_id"] == audit["event_id"]
    assert reopened(database, clock).get(receipt["id"]) == restored


def test_required_001_002_scenario_restores_order_and_quarantines_unchanged_snapshot(protected):
    lab, database, store, clock = protected
    first, first_key = register(store)
    assert first["display_number"].endswith("001")
    clock.value += timedelta(minutes=10)
    seating_args = command_args(store, first)
    false_seating = store.staff_transition(first["id"], "seat", **seating_args)
    invalidated = seating_event(store, first)
    assert false_seating["reception"]["actual_wait_minutes"] == 10
    assert store.get_queue() == []
    clock.value += timedelta(seconds=30)
    second, second_key = register(store)
    assert second["display_number"].endswith("002")
    assert second["snapshot"]["ahead_groups"] == 0
    assert second["snapshot"]["waiting_groups"] == 0
    immutable = intake(lab, database)
    clock.value += timedelta(seconds=30)
    correction = correct(store, first)
    assert correction["outcome"] == "state_changed"
    assert [row["id"] for row in store.get_queue()] == [first["id"], second["id"]]
    assert len(store.get_queue()) == 2
    current_second = store.get(second["id"])
    # Current ahead count is an internal aggregate, not a new public API.
    ahead = sum(row["reception_order"] < current_second["reception_order"] for row in store.get_queue())
    assert ahead == 1
    assert current_second == second
    assert intake(lab, database) == immutable
    source_holds = issues(lab, database, first["id"])
    other_holds = issues(lab, database, second["id"])
    assert {issue["issue_kind"] for issue in source_holds} == {"invalidated_seating"}
    assert {issue["issue_kind"] for issue in other_holds} == {"snapshot_context", "prediction_gate"}
    second_registration = store.get_events(second["id"])[0]
    for issue in other_holds:
        assert issue["evidence_event_id"] == second_registration["event_id"]
        assert issue["correction_event_id"] > second_registration["event_id"] > invalidated["event_id"]
        assert isinstance(json.loads(issue["evidence_json"]), dict)
        assert issue["policy_version"].strip()
    # A -> B -> resend A: the saved seating response is historical only.
    before_retry = database.read_bytes()
    assert store.staff_transition(first["id"], "seat", **seating_args) == false_seating
    assert store.get_staff_operation(seating_args["operation_id"], **ACTOR) == false_seating
    assert database.read_bytes() == before_retry
    assert store.get(first["id"])["status"] == "waiting"
    assert store.get_staff_reception(first["id"])["revision"] == 2
    clock.value += timedelta(minutes=14)
    real_seating = transition(store, first, "seat")
    assert real_seating["reception"]["actual_wait_minutes"] == 25
    assert real_seating["reception"]["seated_at"] == clock.value.isoformat()
    assert [row["id"] for row in store.get_queue()] == [second["id"]]
    assert intake(lab, database) == immutable
    assert issues(lab, database, first["id"]) == source_holds
    assert issues(lab, database, second["id"]) == other_holds
    assert store.register(2, 0, first_key) == (first, False)
    assert store.register(2, 0, second_key) == (second, False)
    for row in (first, second):
        quality = store.get_data_quality(row["id"])
        assert quality["training_use"] == "blocked"
        assert quality["unresolved_issue_ids"]
    assert reopened(database, clock).get(first["id"])["actual_wait_minutes"] == 25


@pytest.mark.parametrize("elapsed", [timedelta(), timedelta(seconds=1, microseconds=500000),
                                     timedelta(days=1, seconds=37)])
def test_reseating_recalculates_zero_fractional_and_cross_date_wait_from_original_intake(protected, elapsed):
    lab, database, store, clock = protected
    row, _ = register(store)
    transition(store, row, "seat")
    correct(store, row)
    clock.value += elapsed
    result = transition(store, row, "seat")
    assert result["reception"]["actual_wait_minutes"] == elapsed.total_seconds() / 60
    assert result["reception"]["received_at"] == row["received_at"]
    assert len(issues(lab, database, row["id"])) == 1
    assert reopened(database, clock).get(row["id"])["actual_wait_minutes"] == elapsed.total_seconds() / 60


def test_lost_correction_response_replays_once_without_revision_event_or_issue_changes(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    transition(store, row, "seat")
    register(store)
    args = correction_args(store, row)
    result = store.correct_seating(row["id"], **args)
    assert result["outcome"] == "state_changed"
    before = database.read_bytes()
    clock.value += timedelta(days=1)
    restarted = reopened(database, clock)
    for _ in range(3):
        assert restarted.correct_seating(row["id"], **args) == result
        assert restarted.get_staff_operation(args["operation_id"], **ACTOR) == result
        assert database.read_bytes() == before
    assert len(store.get_events(row["id"])) == 3
    assert store.get_staff_reception(row["id"])["revision"] == 2
    assert len(issues(lab, database)) == 3


@pytest.mark.parametrize("changed", ["reception", "revision", "epoch", "event", "code", "detail", "principal_kind", "principal_id"])
def test_correction_id_reuse_with_any_changed_request_or_actor_never_writes(protected, changed):
    lab, database, store, clock = protected
    row, _ = register(store)
    other, _ = register(store)
    transition(store, row, "seat")
    transition(store, other, "seat")
    args = correction_args(store, row)
    original = store.correct_seating(row["id"], **args)
    rid = row["id"]
    if changed == "reception":
        rid = other["id"]
    elif changed == "revision":
        args["expected_revision"] += 1
    elif changed == "epoch":
        args["write_epoch"] = str(uuid4())
    elif changed == "event":
        args["target_event_id"] = seating_event(store, other)["event_id"]
    elif changed == "code":
        args["reason_code"] = "wrong_reception"
    elif changed == "detail":
        args["reason_detail"] += "。別の理由"
    elif changed == "principal_kind":
        args["principal_kind"] = "staff_account"
    else:
        args["principal_id"] = "other"
    before = database.read_bytes()
    with pytest.raises(StateConflict):
        store.correct_seating(rid, **args)
    assert database.read_bytes() == before
    assert store.get_staff_operation(original["operation_id"], **ACTOR) == original


@pytest.mark.parametrize("mismatch", ["revision", "epoch", "registration", "other_reception", "missing_event"])
def test_invalid_or_stale_correction_is_archived_without_state_event_or_quality_changes(protected, mismatch):
    lab, database, store, clock = protected
    row, _ = register(store)
    other, _ = register(store)
    transition(store, row, "seat")
    transition(store, other, "seat")
    args = correction_args(store, row)
    if mismatch == "revision":
        args["expected_revision"] = 0
    elif mismatch == "epoch":
        args["write_epoch"] = str(uuid4())
    elif mismatch == "registration":
        args["target_event_id"] = store.get_events(row["id"])[0]["event_id"]
    elif mismatch == "other_reception":
        args["target_event_id"] = seating_event(store, other)["event_id"]
    else:
        args["target_event_id"] = 999999
    current, events = store.get_staff_reception(row["id"]), store.get_events(row["id"])
    quality = issues(lab, database)
    result = store.correct_seating(row["id"], **args)
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert store.get_staff_reception(row["id"]) == current
    assert store.get_events(row["id"]) == events
    assert issues(lab, database) == quality
    assert store.get_staff_operation(args["operation_id"], **ACTOR) == result
    before = database.read_bytes()
    assert store.correct_seating(row["id"], **args) == result
    assert database.read_bytes() == before
    reopened(database, clock)


def test_old_correction_target_is_rejected_after_restore_and_reseating(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    transition(store, row, "seat")
    old_args = correction_args(store, row)
    first = store.correct_seating(row["id"], **old_args)
    # A fresh ID must not reinterpret an old correction as a harmless no-op.
    duplicate = correct(store, row, operation_id=str(uuid4()))
    assert duplicate["outcome"] == "rejected"
    clock.value += timedelta(minutes=2)
    transition(store, row, "seat")
    before = store.get_staff_reception(row["id"])
    old_target = correct(store, row, target_event_id=old_args["target_event_id"])
    assert old_target["outcome"] == "rejected" and old_target["response_status"] == 409
    assert store.get_staff_reception(row["id"]) == before
    before_replay = database.read_bytes()
    assert store.correct_seating(row["id"], **old_args) == first
    assert database.read_bytes() == before_replay
    assert store.get(row["id"])["status"] == "seated"
    assert len(issues(lab, database, row["id"])) == 1
    reopened(database, clock)


@pytest.mark.parametrize("version,mode,enabled", [(1, "legacy", 0), (2, "legacy", 0),
                                                   (2, "revision_required", 0)])
def test_v1_legacy_and_disabled_corrections_are_refused_without_writes(version, mode, enabled):
    with synthetic_store(version=version, mode=mode, enabled=enabled) as (lab, database, store, clock):
        row, _ = register(store)
        if version == 1 or mode == "legacy":
            store.transition(row["id"], "seated")
        else:
            transition(store, row, "seat")
        target = seating_event(store, row)["event_id"]
        if version == 2:
            args = correction_args(store, row)
        else:
            args = dict(operation_id=str(uuid4()), expected_revision=0, write_epoch=str(uuid4()),
                        target_event_id=target, reason_code="wrong_reception",
                        reason_detail="訂正対象を間違えた", **ACTOR)
        before = database.read_bytes()
        with pytest.raises(StateConflict):
            store.correct_seating(row["id"], **args)
        assert database.read_bytes() == before


@pytest.mark.parametrize("overrides", [
    {"target_event_id": 0}, {"target_event_id": -1}, {"target_event_id": True},
    {"target_event_id": "1"}, {"target_event_id": 1.0}, {"target_event_id": 2 ** 63},
    {"reason_code": ""}, {"reason_code": "unknown"}, {"reason_code": None},
    {"reason_detail": ""}, {"reason_detail": "   "}, {"reason_detail": None},
    {"reason_detail": "x" * 501}, {"operation_id": "not-a-uuid"},
    {"expected_revision": True}, {"expected_revision": -1}, {"write_epoch": "bad"},
    {"principal_id": ""},
])
def test_malformed_correction_never_claims_operation_id_or_writes(protected, overrides):
    lab, database, store, clock = protected
    row, _ = register(store)
    transition(store, row, "seat")
    args = correction_args(store, row, **overrides)
    before = database.read_bytes()
    with pytest.raises(StoreValidationError):
        store.correct_seating(row["id"], **args)
    assert database.read_bytes() == before


def test_event_id_interval_includes_equal_timestamp_registrations_and_excludes_outer_ones(protected):
    lab, database, store, clock = protected
    before, _ = register(store)
    target, _ = register(store)
    transition(store, target, "seat")
    during, _ = register(store)
    correct(store, target)
    after, _ = register(store)
    assert len({event["recorded_at"] for row in (before, target, during, after)
                for event in store.get_events(row["id"])}) == 1
    assert issues(lab, database, before["id"]) == []
    assert issues(lab, database, after["id"]) == []
    # The older active party kept the prediction gate open; queue length is not
    # a model feature, so only the saved snapshot context is affected here.
    assert {issue["issue_kind"] for issue in issues(lab, database, during["id"])} == {
        "snapshot_context"}
    assert len(issues(lab, database, target["id"])) == 1
    reopened(database, clock)


@pytest.mark.parametrize("action", ["call", "absent", "return", "seat", "cancel"])
def test_other_workflow_actions_during_wrong_seating_are_quarantined(protected, action):
    lab, database, store, clock = protected
    other, _ = register(store)
    if action in {"absent", "return"}:
        transition(store, other, "call")
    if action == "return":
        transition(store, other, "absent")
    target, _ = register(store)
    transition(store, target, "seat")
    transition(store, other, action)
    affected_event = store.get_events(other["id"])[-1]
    correct(store, target)
    holds = issues(lab, database, other["id"])
    assert len(holds) == 1
    assert holds[0]["issue_kind"] == "workflow_impact"
    assert holds[0]["evidence_event_id"] == affected_event["event_id"]
    reopened(database, clock)


def test_multiple_workflow_events_are_recorded_in_one_issue_without_losing_evidence(protected):
    lab, database, store, clock = protected
    other, _ = register(store)
    target, _ = register(store)
    transition(store, target, "seat")
    evidence_ids = []
    for action in ("call", "absent", "return", "call", "seat"):
        transition(store, other, action)
        evidence_ids.append(store.get_events(other["id"])[-1]["event_id"])
    correct(store, target)
    holds = issues(lab, database, other["id"])
    assert len(holds) == 1 and holds[0]["issue_kind"] == "workflow_impact"
    assert json.loads(holds[0]["evidence_json"])["evidence_event_ids"] == evidence_ids
    reopened(database, clock)


def test_multiple_wrong_seatings_of_same_reception_keep_every_unresolved_invalidated_event(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    invalidated_ids = []
    for _ in range(2):
        transition(store, row, "seat")
        invalidated_ids.append(seating_event(store, row)["event_id"])
        correct(store, row)
    holds = issues(lab, database, row["id"])
    assert len(holds) == 2
    assert [issue["evidence_event_id"] for issue in holds] == invalidated_ids
    assert all(issue["issue_kind"] == "invalidated_seating" for issue in holds)
    transition(store, row, "seat")
    quality = store.get_data_quality(row["id"])
    assert quality["training_use"] == "blocked"
    assert quality["unresolved_issue_ids"] == [issue["issue_id"] for issue in holds]
    reopened(database, clock)


def test_overlapping_corrections_keep_separate_unresolved_causes_for_one_reception(protected):
    lab, database, store, clock = protected
    first, _ = register(store)
    second, _ = register(store)
    transition(store, first, "seat")
    transition(store, second, "seat")
    affected, _ = register(store)
    first_correction = correct(store, first)
    second_correction = correct(store, second)
    holds = issues(lab, database, affected["id"])
    assert len(holds) == 4
    assert sorted(issue["issue_kind"] for issue in holds) == [
        "prediction_gate", "prediction_gate", "snapshot_context", "snapshot_context"]
    assert len({issue["correction_event_id"] for issue in holds}) == 2
    quality = store.get_data_quality(affected["id"])
    assert quality["training_use"] == "blocked"
    assert set(quality["unresolved_issue_ids"]) == {issue["issue_id"] for issue in holds}
    transition(store, affected, "seat")
    assert store.get_data_quality(affected["id"])["training_use"] == "blocked"
    assert set(store.get_data_quality(affected["id"])["unresolved_issue_ids"]) == {
        issue["issue_id"] for issue in holds}
    assert first_correction["operation_id"] != second_correction["operation_id"]
    reopened(database, clock)


def test_unassessed_clean_reception_is_also_blocked_until_extraction_policy_exists(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    transition(store, row, "seat")
    quality = store.get_data_quality(row["id"])
    assert quality["training_use"] == "blocked"
    assert quality["unresolved_issue_ids"] == []
    assert quality["reasons"]


@pytest.mark.parametrize("label", ["zero", "fraction", "overnight"])
def test_imported_v1_seating_event_can_be_corrected_from_revision_zero_without_rewriting_legacy_history(label):
    with temporary_workspace() as lab:
        database, ids = create_v1(lab, "imported.sqlite3", populated=True)
        migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        with lab.connection(database) as conn:
            conn.execute("UPDATE store_metadata SET staff_write_mode='revision_required',seat_corrections_enabled=1")
        clock = Clock()
        store = ReceptionStore(database, clock=clock)
        row = store.get(ids[label])
        old_events = store.get_events(row["id"])
        assert old_events[-1]["audit_kind"] == "legacy_v1"
        assert store.get_staff_reception(row["id"])["revision"] == 0
        immutable = intake(lab, database)
        result = correct(store, row)
        assert result["outcome"] == "state_changed"
        assert result["resulting_revision"] == 1
        assert result["reception"]["status"] == old_events[-1]["from_status"] == "waiting"
        assert result["reception"]["seated_at"] is None
        assert result["reception"]["actual_wait_minutes"] is None
        assert store.get_events(row["id"])[:-1] == old_events
        assert intake(lab, database) == immutable
        assert issues(lab, database, row["id"])[0]["issue_kind"] == "invalidated_seating"
        reopened(database, clock)


def test_normal_operation_id_cannot_be_reused_for_a_correction(protected):
    lab, database, store, clock = protected
    row, _ = register(store)
    seating = transition(store, row, "seat")
    before = database.read_bytes()
    with pytest.raises(StateConflict):
        correct(store, row, operation_id=seating["operation_id"])
    assert database.read_bytes() == before


def test_silent_omission_of_a_required_quality_issue_cannot_commit_partial_correction(protected, monkeypatch):
    lab, database, store, clock = protected
    row, _ = register(store)
    transition(store, row, "seat")
    register(store)
    args = correction_args(store, row)
    original = store._save_quality_issues
    def incomplete(conn, expected):
        assert len(expected) >= 3
        original(conn, expected[:-1])
    monkeypatch.setattr(store, "_save_quality_issues", incomplete)
    before = database.read_bytes()
    with pytest.raises(StoreValidationError):
        store.correct_seating(row["id"], **args)
    assert database.read_bytes() == before
    monkeypatch.setattr(store, "_save_quality_issues", original)
    assert store.correct_seating(row["id"], **args)["outcome"] == "state_changed"
    reopened(database, clock)


@pytest.mark.parametrize("failure_stage", ["before_event", "after_event", "before_quality",
                                          "after_quality", "after_ledger", "before_commit"])
def test_failure_at_every_correction_stage_rolls_back_all_tables_sequences_and_clock(protected, monkeypatch, failure_stage):
    lab, database, store, clock = protected
    row, _ = register(store)
    transition(store, row, "seat")
    register(store)
    args = correction_args(store, row)
    append, quality, save, connection = (store._append_correction_event, store._save_quality_issues,
                                        store._save_staff_operation, store._connection)
    before = database.read_bytes()
    if failure_stage == "before_commit":
        @contextmanager
        def broken(*, write=False):
            with connection(write=write) as conn:
                yield conn
                if write:
                    raise RuntimeError("injected correction failure before commit")
        monkeypatch.setattr(store, "_connection", broken)
    else:
        method = ("_append_correction_event" if "event" in failure_stage else
                  "_save_quality_issues" if "quality" in failure_stage else "_save_staff_operation")
        original = getattr(store, method)
        def broken(*a, **kw):
            if failure_stage.startswith("after_"):
                original(*a, **kw)
            raise RuntimeError("injected correction failure")
        monkeypatch.setattr(store, method, broken)
    with pytest.raises(RuntimeError, match="injected correction failure"):
        store.correct_seating(row["id"], **args)
    assert database.read_bytes() == before
    assert store.get(row["id"])["status"] == "seated"
    assert issues(lab, database) == []
    monkeypatch.setattr(store, "_append_correction_event", append)
    monkeypatch.setattr(store, "_save_quality_issues", quality)
    monkeypatch.setattr(store, "_save_staff_operation", save)
    monkeypatch.setattr(store, "_connection", connection)
    result = store.correct_seating(row["id"], **args)
    assert result["outcome"] == "state_changed"
    assert store.get_staff_reception(row["id"])["revision"] == 2
    assert len(issues(lab, database)) == 3
    reopened(database, clock)


@pytest.mark.parametrize("same_id", [True, False])
def test_concurrent_corrections_commit_once_with_one_quality_set(protected, same_id):
    lab, database, store, clock = protected
    row, _ = register(store)
    transition(store, row, "seat")
    register(store)
    args = correction_args(store, row)
    stores = [ReceptionStore(database, clock=clock) for _ in range(4)]
    barrier = Barrier(len(stores))
    def worker(other):
        barrier.wait(timeout=10)
        command = args if same_id else {**args, "operation_id": str(uuid4())}
        return other.correct_seating(row["id"], **command)
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = list(pool.map(worker, stores))
    if same_id:
        assert all(result == results[0] for result in results)
        assert all(result["outcome"] == "state_changed" for result in results)
    else:
        assert [result["outcome"] for result in results].count("state_changed") == 1
        assert [result["outcome"] for result in results].count("rejected") == 3
    assert len(store.get_events(row["id"])) == 3
    assert len(issues(lab, database)) == 3
    assert store.get_staff_reception(row["id"])["revision"] == 2
    reopened(database, clock)


@pytest.mark.parametrize("target", ["called", "absent", "waiting", "seated", "cancelled"])
def test_legacy_transition_cannot_bypass_required_mode_after_correction(protected, target):
    lab, database, store, clock = protected
    row, _ = register(store)
    transition(store, row, "seat")
    correct(store, row)
    before = database.read_bytes()
    with pytest.raises(StateConflict):
        store.transition(row["id"], target)
    assert database.read_bytes() == before
