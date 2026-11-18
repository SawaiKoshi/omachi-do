"""Replay correction history and reject incomplete quality protection before writes.

All databases are synthetic and created by the private /tmp migration lab.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_schema import validate_history, validate_schema
from reception_store import ReceptionStore, StoreValidationError


ACTOR = {"principal_kind": "shared_staff_token", "principal_id": "shared-history-test"}


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 29, 18, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


def prediction(**kwargs):
    return {"status": "available", "minutes": 12.125, "model_version": "synthetic-only",
            "predicted_at": kwargs["received_at"].isoformat(), "warnings": []}


def change(store, row, action):
    current = store.get_staff_reception(row["id"])
    return store.staff_transition(row["id"], action, operation_id=str(uuid4()),
                                  expected_revision=current["revision"],
                                  write_epoch=current["write_epoch"], **ACTOR)


def correct(store, row):
    current = store.get_staff_reception(row["id"])
    return store.correct_seating(row["id"], operation_id=str(uuid4()),
                                 expected_revision=current["revision"],
                                 write_epoch=current["write_epoch"],
                                 target_event_id=store.get_events(row["id"])[-1]["event_id"],
                                 reason_code="accidental_seating", reason_detail="Synthetic mistake.",
                                 **ACTOR)


@contextmanager
def correction_history(original="waiting"):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, "correction-history.sqlite3", populated=False)
        migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        with lab.connection(database) as conn:
            conn.execute("UPDATE store_metadata SET staff_write_mode='revision_required', "
                         "seat_corrections_enabled=1")
        clock = Clock()
        store = ReceptionStore(database, clock=clock)
        first, _ = store.register(2, 0, str(uuid4()), predictor=prediction)
        if original in ("called", "absent"):
            clock.value += timedelta(minutes=1)
            change(store, first, "call")
        if original == "absent":
            clock.value += timedelta(minutes=1)
            change(store, first, "absent")
        before_seating = store.get(first["id"])
        clock.value = clock.value.replace(hour=18, minute=10, second=0)
        change(store, first, "seat")
        clock.value += timedelta(seconds=30)
        second, _ = store.register(3, 0, str(uuid4()), predictor=prediction)
        clock.value += timedelta(seconds=30)
        result = correct(store, first)
        yield lab, database, store, clock, first, second, before_seating, result


def assert_history_valid(lab, database):
    with lab.connection(database, readonly=True) as conn:
        validate_schema(conn, 2, allow_revision_required=True)
        validate_history(conn, 2)


def damage(lab, database, table, sql, args=(), *, delete=False):
    """Change only a disposable fixture, then restore the exact known trigger DDL."""
    suffix = "delete" if delete else "update"
    trigger_name = f"{table}_no_{suffix}"
    with lab.connection(database) as conn:
        trigger = conn.execute("SELECT sql FROM sqlite_schema WHERE name=?", (trigger_name,)).fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f'DROP TRIGGER "{trigger_name}"')
            conn.execute(sql, args)
            conn.execute(trigger)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        validate_schema(conn, 2, allow_revision_required=True)


def assert_reopen_refused_without_writing(database, clock):
    damaged = database.read_bytes()
    with pytest.raises(StoreValidationError):
        ReceptionStore(database, clock=clock)
    assert database.read_bytes() == damaged


@pytest.mark.parametrize("original", ["waiting", "called", "absent"])
def test_correction_replay_preserves_original_state_and_historical_snapshot(original):
    with correction_history(original) as (lab, database, store, clock, first, second, before, result):
        assert result["response_status"] == 200
        after = store.get(first["id"])
        assert after["status"] == original
        assert after["called_at"] == before["called_at"]
        assert after["seated_at"] is None and after["actual_wait_minutes"] is None
        assert second["snapshot"]["waiting_groups"] == 0
        assert_history_valid(lab, database)
        restarted = ReceptionStore(database, clock=clock)
        assert restarted.get(first["id"]) == after
        assert restarted.get(second["id"])["snapshot"] == second["snapshot"]
        clock.value += timedelta(minutes=1)
        third, _ = restarted.register(1, 0, str(uuid4()), predictor=prediction)
        assert third["snapshot"]["waiting_groups"] == 2
        clock.value = clock.value.replace(hour=18, minute=25, second=0)
        change(restarted, first, "seat")
        assert restarted.get(first["id"])["actual_wait_minutes"] == 25
        assert_history_valid(lab, database)
        ReceptionStore(database, clock=clock)


@pytest.mark.parametrize("kind", ["invalidated_seating", "snapshot_context", "prediction_gate"])
def test_missing_any_required_quality_issue_is_rejected_on_reopen(kind):
    with correction_history() as (lab, database, _, clock, *_):
        damage(lab, database, "data_quality_issues",
               "DELETE FROM data_quality_issues WHERE issue_kind=?", (kind,), delete=True)
        assert_reopen_refused_without_writing(database, clock)


@pytest.mark.parametrize("case", ["policy", "evidence_json", "evidence_event_id", "recorded_at"])
def test_changed_quality_policy_evidence_or_timestamp_is_rejected(case):
    with correction_history() as (lab, database, _, clock, first, second, *_):
        columns = {
            "policy": ("policy_version", "unknown-policy"),
            "evidence_json": ("evidence_json", '{"correction_event_id":1,"evidence_event_ids":[],"target_event_id":1}'),
            "evidence_event_id": ("evidence_event_id", None),
            "recorded_at": ("recorded_at", second["received_at"]),
        }
        column, value = columns[case]
        damage(lab, database, "data_quality_issues",
               f"UPDATE data_quality_issues SET {column}=? WHERE reception_id=?",
               (value, first["id"]))
        assert_reopen_refused_without_writing(database, clock)


def test_extra_quality_issue_with_a_valid_foreign_key_is_rejected():
    with correction_history() as (lab, database, store, clock, first, *_):
        correction = store.get_events(first["id"])[-1]
        with lab.connection(database) as conn:
            conn.execute("INSERT INTO data_quality_issues(reception_id,correction_event_id,issue_kind,"
                         "evidence_event_id,policy_version,evidence_json,recorded_at) "
                         "VALUES(?,?,'workflow_impact',?,'seat-correction-v1','{}',?)",
                         (first["id"], correction["event_id"], correction["event_id"], correction["recorded_at"]))
        assert_reopen_refused_without_writing(database, clock)


@pytest.mark.parametrize("field,value", [
    ("reason_detail", "Different recorded reason."), ("reason_code", "wrong_reception"),
])
def test_correction_audit_reason_must_match_its_saved_request(field, value):
    with correction_history() as (lab, database, store, clock, first, *_):
        correction = store.get_events(first["id"])[-1]
        damage(lab, database, "reception_events",
               f"UPDATE reception_events SET {field}=? WHERE event_id=?", (value, correction["event_id"]))
        assert_reopen_refused_without_writing(database, clock)


def test_correction_to_called_keeps_the_original_call_timestamp_in_audit_state():
    with correction_history("called") as (lab, database, store, clock, first, *_):
        correction = store.get_events(first["id"])[-1]
        projected = json.loads(correction["after_state_json"])
        projected["called_at"] = correction["occurred_at"]
        damage(lab, database, "reception_events",
               "UPDATE reception_events SET after_state_json=? WHERE event_id=?",
               (json.dumps(projected), correction["event_id"]))
        assert_reopen_refused_without_writing(database, clock)


@pytest.mark.parametrize("field", ["children", "snapshot_count"])
def test_archived_correction_result_cannot_replace_integer_zero_with_false(field):
    with correction_history() as (lab, database, store, clock, first, *_):
        correction = store.get_events(first["id"])[-1]
        with lab.connection(database, readonly=True) as conn:
            encoded = conn.execute("SELECT response_json FROM staff_operations WHERE operation_id=?",
                                   (correction["operation_id"],)).fetchone()[0]
        response = json.loads(encoded)
        if field == "children":
            response["reception"]["children"] = False
        else:
            response["reception"]["snapshot"]["waiting_groups"] = False
        damage(lab, database, "staff_operations",
               "UPDATE staff_operations SET response_json=? WHERE operation_id=?",
               (json.dumps(response), correction["operation_id"]))
        assert_reopen_refused_without_writing(database, clock)


def test_multiple_correction_cycles_each_require_their_own_quality_issue():
    with correction_history() as (lab, database, store, clock, first, *_):
        clock.value += timedelta(minutes=1)
        change(store, first, "seat")
        clock.value += timedelta(minutes=1)
        correct(store, first)
        assert_history_valid(lab, database)
        with lab.connection(database, readonly=True) as conn:
            corrections = conn.execute("SELECT event_id FROM reception_events "
                                       "WHERE event_type='seating_corrected' ORDER BY event_id").fetchall()
        assert len(corrections) == 2
        damage(lab, database, "data_quality_issues",
               "DELETE FROM data_quality_issues WHERE correction_event_id=?",
               (corrections[0][0],), delete=True)
        assert_reopen_refused_without_writing(database, clock)


def test_disabling_future_corrections_does_not_invalidate_past_correction_history():
    with correction_history() as (lab, database, _, clock, *_):
        with lab.connection(database) as conn:
            conn.execute("UPDATE store_metadata SET seat_corrections_enabled=0")
        assert_history_valid(lab, database)
        ReceptionStore(database, clock=clock)
