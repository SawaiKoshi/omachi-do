"""A restored synthetic backup must not execute commands from its old epoch."""

from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from reception_migration.migration import copy_database, migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_store import ReceptionNotFound, ReceptionStore, StateConflict


ACTOR = {"principal_kind": "shared_staff_token", "principal_id": "shared-staff"}


def intake_rows(lab, database):
    with lab.connection(database, readonly=True) as conn:
        return {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                for table in ("reception_snapshots", "predictions", "idempotency_keys",
                              "daily_counters")}


def test_restored_backup_with_missing_successful_command_requires_a_new_epoch_and_operation():
    with temporary_workspace() as lab:
        original, _ = create_v1(lab, "original.sqlite3", populated=False)
        migrate(lab, original, expected_sha256=sha256(original.read_bytes()).hexdigest())
        with lab.connection(original) as conn:
            conn.execute("UPDATE store_metadata SET staff_write_mode='revision_required'")
        now = datetime(2026, 10, 29, 12, tzinfo=ZoneInfo("Asia/Tokyo"))
        store = ReceptionStore(original, clock=lambda: now)
        key = str(uuid4())
        receipt, _ = store.register(2, 1, key)
        before = store.get_staff_reception(receipt["id"])
        call_args = dict(operation_id=str(uuid4()), expected_revision=0,
                         write_epoch=before["write_epoch"], **ACTOR)
        call = store.staff_transition(receipt["id"], "call", **call_args)
        immutable = intake_rows(lab, original)
        backup = copy_database(lab, original, "backup.sqlite3")
        backup_bytes = backup.read_bytes()

        # This successful command and its response are newer than the backup.
        seat_args = dict(operation_id=str(uuid4()), expected_revision=1,
                         write_epoch=before["write_epoch"], **ACTOR)
        now += timedelta(minutes=1)
        seated = store.staff_transition(receipt["id"], "seat", **seat_args)
        assert seated["reception"]["actual_wait_minutes"] == 1
        original_bytes = original.read_bytes()

        restored = copy_database(lab, backup, "restored.sqlite3")
        restored_epoch = str(uuid4())
        with lab.connection(restored) as conn:
            conn.execute("UPDATE store_metadata SET write_epoch=?", (restored_epoch,))
        now += timedelta(minutes=2)
        recovered = ReceptionStore(restored, clock=lambda: now)
        current = recovered.get_staff_reception(receipt["id"])
        assert current["revision"] == 1
        assert current["reception"]["status"] == "called"
        assert current["write_epoch"] == restored_epoch
        with pytest.raises(ReceptionNotFound):
            recovered.get_staff_operation(seat_args["operation_id"], **ACTOR)

        # Results retained in the backup remain historical, without any write.
        restored_bytes = restored.read_bytes()
        assert recovered.staff_transition(receipt["id"], "call", **call_args) == call
        assert recovered.get_staff_operation(call_args["operation_id"], **ACTOR) == call
        assert restored.read_bytes() == restored_bytes

        events = recovered.get_events(receipt["id"])
        rejected = recovered.staff_transition(receipt["id"], "seat", **seat_args)
        assert rejected["outcome"] == "rejected"
        assert rejected["response_status"] == 409
        assert rejected["error"]["code"] == "write_epoch_conflict"
        assert recovered.get_staff_reception(receipt["id"]) == current
        assert recovered.get_events(receipt["id"]) == events
        rejected_bytes = restored.read_bytes()
        assert recovered.staff_transition(receipt["id"], "seat", **seat_args) == rejected
        assert restored.read_bytes() == rejected_bytes
        # Changing the request while retaining its claimed ID is also forbidden.
        with pytest.raises(StateConflict):
            recovered.staff_transition(receipt["id"], "seat",
                                       **{**seat_args, "write_epoch": restored_epoch})
        assert restored.read_bytes() == rejected_bytes

        accepted = recovered.staff_transition(
            receipt["id"], "seat", **{**seat_args, "operation_id": str(uuid4()),
                                       "write_epoch": restored_epoch})
        assert accepted["outcome"] == "state_changed"
        assert accepted["resulting_revision"] == 2
        assert accepted["reception"]["actual_wait_minutes"] == 3
        assert len(recovered.get_events(receipt["id"])) == 3
        assert recovered.register(2, 1, key) == (receipt, False)
        assert intake_rows(lab, restored) == immutable == intake_rows(lab, original)
        # Reopening performs the full schema/history checks on restored results.
        assert ReceptionStore(restored, clock=lambda: now).get(receipt["id"])["status"] == "seated"
        assert backup.read_bytes() == backup_bytes
        assert original.read_bytes() == original_bytes
        with lab.connection(restored, readonly=True) as conn:
            assert conn.execute("SELECT seat_corrections_enabled FROM store_metadata").fetchone() == (0,)
