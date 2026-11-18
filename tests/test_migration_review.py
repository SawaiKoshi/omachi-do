"""Regressions found while reviewing the migration before runtime integration."""

from hashlib import sha256
import os
import sqlite3

import pytest

from reception_migration.migration import MigrationError, migrate, validate_schema
from reception_migration.safety import MARKER, SafetyError, temporary_workspace
from reception_migration.schema import signature
from reception_migration.synthetic import BASE, create_v1


@pytest.mark.parametrize("version", [1, 2])
def test_validation_does_not_depend_on_sqlite_row_factory(version):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        with lab.connection(database) as conn:
            conn.row_factory = sqlite3.Row
            validate_schema(conn, version)


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_manifest_replacement_does_not_write_another_file(link):
    with temporary_workspace() as lab, temporary_workspace() as other:
        database, _ = create_v1(lab)
        target = other.new_file("untouched.sqlite3")
        target.write_bytes(b"synthetic protected target")
        original = target.read_bytes()
        marker = lab.root / MARKER
        marker.unlink()
        if link == "symlink":
            marker.symlink_to(target)
        else:
            os.link(target, marker)
        with pytest.raises((SafetyError, OSError)):
            lab.authorize_synthetic(database)
        assert target.read_bytes() == original


def test_sql_signature_preserves_whitespace_inside_quoted_values():
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, populated=False)
        with lab.connection(database) as conn:
            conn.execute("CREATE TABLE example(value TEXT CHECK(value='a b'))")
            first = signature(conn)
            conn.execute("DROP TABLE example")
            conn.execute("CREATE TABLE example(value TEXT CHECK(value='a  b'))")
            assert signature(conn) != first


@pytest.mark.parametrize("fault", ["empty_sequence", "counter_type", "event_before_intake",
                                  "event_clock_reversal", "snapshot", "key_people"])
def test_inconsistent_recorded_history_is_rejected_without_migration(fault):
    with temporary_workspace() as lab:
        database, ids = create_v1(lab, populated=fault != "empty_sequence")
        with lab.connection(database) as conn:
            if fault == "empty_sequence":
                conn.execute("INSERT INTO sqlite_sequence VALUES('receptions',-1)")
            elif fault == "counter_type":
                conn.execute("UPDATE daily_counters SET last_number=100.5")
            elif fault == "event_before_intake":
                conn.execute("UPDATE reception_events SET occurred_at='2026-10-28T23:58:00+09:00' "
                             "WHERE reception_id=? AND event_type='called'", (ids["called"],))
            elif fault == "event_clock_reversal":
                conn.execute("UPDATE reception_events SET occurred_at=?,recorded_at=? "
                             "WHERE reception_id=? AND event_type='cancelled'",
                             (BASE.isoformat(), BASE.isoformat(), ids["cancelled"]))
            elif fault == "snapshot":
                conn.execute("UPDATE reception_snapshots SET waiting_groups=0,ahead_groups=0,"
                             "ahead_same_size_groups=0,groups_1_2=0,groups_3_4=0,groups_5_plus=0 "
                             "WHERE reception_id=?", (ids["called"],))
            else:
                conn.execute("UPDATE idempotency_keys SET adults=99 WHERE reception_id=?", (ids["waiting"],))
        lab.authorize_synthetic(database)
        original = database.read_bytes()
        with pytest.raises(MigrationError):
            migrate(lab, database, expected_sha256=sha256(original).hexdigest())
        assert database.read_bytes() == original


@pytest.mark.parametrize("name", ["source.sqlite3-journal", "source.sqlite3-wal", "source.sqlite3-shm", MARKER])
def test_sqlite_sidecar_names_are_not_accepted_as_database_names(name):
    with temporary_workspace() as lab:
        with pytest.raises(SafetyError):
            lab.new_file(name)


def test_wal_format_is_rejected_before_sqlite_opens_the_source(monkeypatch):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab)
        with lab.connection(database) as conn:
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        original = database.read_bytes()
        def forbidden(*args, **kwargs):
            pytest.fail("WAL source should be rejected before sqlite3.connect")
        monkeypatch.setattr(sqlite3, "connect", forbidden)
        with pytest.raises(SafetyError, match="rollback-journal"):
            migrate(lab, database, expected_sha256=sha256(original).hexdigest())
        assert database.read_bytes() == original
