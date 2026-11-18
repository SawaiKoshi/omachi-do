"""The independent SQLite boundary returns no unapproved feature/label rows.

Every database, damaged fixture and WAL writer lives in a private /tmp workspace.
No real model, Excel, stored operational DB or backup is opened by these tests.
"""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import re
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_store import ReceptionStore
from sqlite_training_extract import ExtractionValidationError, extract_candidates


ACTOR = {"principal_kind": "shared_staff_token", "principal_id": "synthetic-extract-staff"}
VERIFIED = {"principal_kind": "staff_account", "principal_id": "synthetic-extract-reviewer"}


class SyntheticAuthority:
    """Explicitly test-only; production has no verified-reviewer integration."""

    authority_id = "synthetic-extract-authority"
    dataset_contract = "rf-intake-actual-wait-v1"

    def verify(self, principal_kind, principal_id, permission, evidence, context):
        return (dict(principal_kind=principal_kind, principal_id=principal_id) == VERIFIED
                and permission in {"clear", "eligible", "extract"}
                and evidence["reference"] == "synthetic/source-confirmation")


def evidence():
    return dict(reference="synthetic/source-confirmation", intake_verified=True,
                seating_verified=True, context_understood=True)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 30, 18, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


def prediction(**values):
    return {"status": "available", "minutes": 17.125, "model_version": "synthetic-only",
            "predicted_at": values["received_at"].isoformat(), "warnings": []}


@contextmanager
def environment(*, version=2):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, "training-extraction.sqlite3", populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
            with lab.connection(database) as connection:
                connection.execute("UPDATE store_metadata SET staff_write_mode='revision_required',"
                                   "seat_corrections_enabled=1")
        clock = Clock()
        yield lab, database, ReceptionStore(database, clock=clock), clock


def register(store, adults=2):
    return store.register(adults, 0, str(uuid4()), predictor=prediction)[0]


def transition(store, reception, action):
    current = store.get_staff_reception(reception["id"])
    return store.staff_transition(reception["id"], action, operation_id=str(uuid4()),
                                  expected_revision=current["revision"],
                                  write_epoch=current["write_epoch"], **ACTOR)


def correction(store, reception):
    current = store.get_staff_reception(reception["id"])
    return store.correct_seating(
        reception["id"], operation_id=str(uuid4()), expected_revision=current["revision"],
        write_epoch=current["write_epoch"],
        target_event_id=store.get_events(reception["id"])[-1]["event_id"],
        reason_code="accidental_seating", reason_detail="Synthetic extraction test.", **ACTOR,
    )


def approve(quality, reception, *, decision="eligible"):
    current = quality.context(reception["id"])
    return quality.assess(
        reception["id"], operation_id=str(uuid4()), expected_revision=current["revision"],
        write_epoch=current["write_epoch"], expected_assessment_id=current["latest_assessment_id"],
        expected_watermarks=current["watermarks"], decision=decision,
        reason_code="source_verified", reason_detail="Synthetic documentary confirmation.",
        evidence=evidence() if decision == "eligible" else {}, **VERIFIED,
    )


def review(quality, reception, issue_id, *, decision="clear"):
    current = quality.context(reception["id"])
    issue = next(issue for issue in current["issues"] if issue["issue_id"] == issue_id)
    latest = issue["latest_review"]
    return quality.review_issue(
        reception["id"], operation_id=str(uuid4()), expected_revision=current["revision"],
        write_epoch=current["write_epoch"], expected_watermarks=current["watermarks"],
        issue_id=issue_id, expected_review_number=latest["review_number"] if latest else 0,
        decision=decision, reason_code="source_verified",
        reason_detail="Synthetic documentary confirmation scoped to baseline intake features.",
        evidence=evidence() if decision == "clear" else {}, **VERIFIED,
    )


def approved_store(store):
    from quality_review_store import QualityReviewStore

    return QualityReviewStore(store, authority=SyntheticAuthority())


def test_empty_v2_produces_only_snapshot_provenance_and_empty_lists():
    with environment() as (_, database, _, clock):
        before = database.read_bytes()
        result = extract_candidates(database, clock=clock)
        assert result["candidates"] == [] and result["excluded"] == []
        assert set(result) == {"snapshot", "candidates", "excluded"}
        assert set(result["snapshot"]) == {
            "database_id", "write_epoch", "event_id", "issue_id", "review_id", "assessment_id",
            "extracted_at", "policy_version", "dataset_contract",
        }
        for name in ("event_id", "issue_id", "review_id", "assessment_id"):
            assert result["snapshot"][name] == 0
        assert result["snapshot"]["extracted_at"] == clock.value.isoformat()
        assert result["snapshot"]["dataset_contract"] == "rf-intake-actual-wait-v1"
        assert database.read_bytes() == before


def test_missing_file_is_refused_without_creating_database(tmp_path):
    database = tmp_path / "must-not-create.sqlite3"
    with pytest.raises(ExtractionValidationError):
        extract_candidates(database)
    assert not database.exists()
    assert list(tmp_path.iterdir()) == []


def test_v1_database_is_refused_without_migration_or_writes():
    with environment(version=1) as (_, database, store, _):
        register(store)
        before = database.read_bytes()
        with pytest.raises(ExtractionValidationError):
            extract_candidates(database)
        assert database.read_bytes() == before


@pytest.mark.parametrize("damage", ["version", "table", "metadata"])
def test_unknown_or_incomplete_schema_never_returns_candidates(damage):
    with environment() as (lab, database, store, _):
        register(store)
        with lab.connection(database) as connection:
            if damage == "version":
                connection.execute("PRAGMA user_version=999")
            elif damage == "table":
                connection.execute("DROP TABLE data_quality_reviews")
            else:
                connection.execute("DROP TRIGGER metadata_no_delete")
                connection.execute("DELETE FROM store_metadata")
        before = database.read_bytes()
        with pytest.raises(ExtractionValidationError):
            extract_candidates(database)
        assert database.read_bytes() == before


@pytest.mark.parametrize("status", ["waiting", "called", "absent", "seated", "cancelled"])
def test_default_authority_excludes_every_state_and_does_not_expose_training_payload(status):
    with environment() as (_, database, store, clock):
        reception = register(store)
        if status in ("called", "absent"):
            transition(store, reception, "call")
        if status == "absent":
            transition(store, reception, "absent")
        if status in ("seated", "cancelled"):
            clock.value += timedelta(minutes=25)
            transition(store, reception, "seat" if status == "seated" else "cancel")
        before = database.read_bytes()
        result = extract_candidates(database, clock=clock)
        assert result["candidates"] == []
        assert len(result["excluded"]) == 1
        excluded = result["excluded"][0]
        assert set(excluded) == {"reception_id", "reasons"}
        assert excluded["reception_id"] == reception["id"] and excluded["reasons"]
        assert database.read_bytes() == before


@pytest.mark.parametrize("now", [None, "2026-10-30", datetime(2026, 10, 30), 123, True])
def test_invalid_clock_cannot_publish_a_partly_built_extraction(now):
    with environment() as (_, database, store, _):
        register(store)
        before = database.read_bytes()
        with pytest.raises(ExtractionValidationError):
            extract_candidates(database, clock=lambda: now)
        assert database.read_bytes() == before


def test_read_connection_uses_explicit_transaction_and_never_executes_dml(monkeypatch):
    import sqlite_training_extract

    statements = []
    original = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        if args[0] != ":memory:":
            assert args[0].endswith("?mode=ro") and kwargs["uri"] is True
            connection.set_trace_callback(statements.append)
        return connection

    with environment() as (_, database, store, clock):
        register(store)
        with monkeypatch.context() as patched:
            patched.setattr(sqlite_training_extract.sqlite3, "connect", traced_connect)
            extract_candidates(database, clock=clock)
        assert "BEGIN" in statements and "COMMIT" in statements
        assert any(sql == "PRAGMA query_only=ON" for sql in statements)
        assert any(sql == "PRAGMA foreign_keys=ON" for sql in statements)
        assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\b", sql,
                                re.IGNORECASE) for sql in statements)


def test_checkpoint_failure_aborts_without_any_result_or_database_change():
    class InjectedFailure(Exception):
        pass

    with environment() as (_, database, store, clock):
        register(store)
        before = database.read_bytes()

        def fail(stage):
            assert stage == "snapshot_acquired"
            raise InjectedFailure

        with pytest.raises(InjectedFailure):
            extract_candidates(database, clock=clock, checkpoint=fail)
        assert database.read_bytes() == before
        assert len(extract_candidates(database)["excluded"]) == 1


def test_one_snapshot_excludes_concurrently_committed_registration_until_next_extract():
    with environment() as (_, database, store, clock):
        # WAL is used only for this disposable DB: a writer may commit while the
        # extractor retains its prior read snapshot. Migration-lab connections
        # intentionally refuse WAL, so no lab.connection is used after this.
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        registered = []

        def write_after_snapshot(stage):
            assert stage == "snapshot_acquired"
            registered.append(register(store))

        first = extract_candidates(database, clock=clock, checkpoint=write_after_snapshot)
        assert first["excluded"] == [] and first["candidates"] == []
        assert first["snapshot"]["event_id"] == 0
        second = extract_candidates(database, clock=clock)
        assert second["snapshot"]["event_id"] == 1
        assert second["excluded"][0]["reception_id"] == registered[0]["id"]


def test_policy_helper_cannot_accidentally_allow_a_row_without_explicit_assessment(monkeypatch):
    import quality_policy

    with environment() as (_, database, store, clock):
        reception = register(store)
        transition(store, reception, "seat")
        before = database.read_bytes()
        with monkeypatch.context() as patched:
            patched.setattr(quality_policy, "candidate_reasons", lambda *args, **kwargs: [])
            with pytest.raises(ExtractionValidationError):
                extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert database.read_bytes() == before


@pytest.mark.parametrize("missing_kind", ["invalidated_seating", "snapshot_context", "prediction_gate"])
def test_missing_correction_quality_coverage_stops_entire_extraction(missing_kind):
    with environment() as (lab, database, store, clock):
        first = register(store)
        clock.value += timedelta(minutes=10)
        transition(store, first, "seat")
        clock.value += timedelta(seconds=30)
        register(store, adults=3)
        clock.value += timedelta(seconds=30)
        correction(store, first)
        with lab.connection(database) as connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='data_quality_issues_no_delete'"
            ).fetchone()[0]
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TRIGGER data_quality_issues_no_delete")
            connection.execute("DELETE FROM data_quality_issues WHERE issue_kind=?", (missing_kind,))
            connection.execute(trigger)
            connection.commit()
        before = database.read_bytes()
        with pytest.raises(ExtractionValidationError):
            extract_candidates(database, clock=clock)
        assert database.read_bytes() == before


@pytest.mark.parametrize("elapsed", [timedelta(0), timedelta(seconds=75, microseconds=125000),
                                     timedelta(days=1, seconds=15)])
def test_approved_exact_labels_keep_zero_fractional_and_overnight_waits(elapsed):
    with environment() as (_, database, store, clock):
        reception = register(store)
        clock.value += elapsed
        transition(store, reception, "seat")
        quality = approved_store(store)
        accepted = approve(quality, reception)
        assert accepted["outcome"] == "assessment_added"
        before = database.read_bytes()
        result = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert result["excluded"] == [] and len(result["candidates"]) == 1
        candidate = result["candidates"][0]
        assert candidate["actual_wait_minutes"] == elapsed.total_seconds() / 60
        assert candidate["received_at"] == reception["received_at"]
        assert candidate["seated_at"] == clock.value.isoformat()
        assert candidate["valid_seated_event_id"] == store.get_events(reception["id"])[-1]["event_id"]
        assert candidate["assessment_id"] == accepted["quality_result"]["assessment_id"]
        assert candidate["reception_revision"] == accepted["quality_result"]["reception_revision"]
        assert candidate["reception_revision"] == store.get_staff_reception(reception["id"])["revision"]
        assert set(candidate) == {
            "id", "display_number", "reception_order", "received_at", "seated_at", "adults",
            "children", "total_people", "actual_wait_minutes", "valid_seated_event_id",
            "reception_revision", "assessment_id", "policy_version", "dataset_contract", "extracted_at",
        }
        assert not {"snapshot", "prediction", "request_json", "principal_id"} & set(candidate)
        assert database.read_bytes() == before
        # Approval rows in SQLite cannot activate production's absent authority.
        assert extract_candidates(database, clock=clock)["candidates"] == []


def test_required_001_002_scenario_filters_unreviewed_partial_stale_and_approved_records():
    with environment() as (lab, database, store, clock):
        first = register(store)
        clock.value += timedelta(minutes=10)
        transition(store, first, "seat")
        original_seat = store.get_events(first["id"])[-1]["event_id"]
        clock.value += timedelta(seconds=30)
        second = register(store, adults=3)
        clock.value += timedelta(seconds=30)
        correction(store, first)
        clock.value += timedelta(minutes=14)
        transition(store, first, "seat")
        quality = approved_store(store)
        assert store.get(first["id"])["actual_wait_minutes"] == 25
        first_issues = quality.context(first["id"])["issues"]
        second_issues = quality.context(second["id"])["issues"]
        assert {issue["issue_kind"] for issue in first_issues} == {"invalidated_seating"}
        assert {issue["issue_kind"] for issue in second_issues} == {"snapshot_context", "prediction_gate"}
        with lab.connection(database, readonly=True) as connection:
            original = {table: list(connection.execute(f"SELECT * FROM {table}"))
                        for table in ("reception_snapshots", "predictions", "idempotency_keys")}
        unreviewed = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert unreviewed["candidates"] == [] and len(unreviewed["excluded"]) == 2
        # Re-seating alone leaves both quality histories unresolved.
        assert all(issue["latest_review"] is None for issue in first_issues + second_issues)
        assert review(quality, first, first_issues[0]["issue_id"])["outcome"] == "review_added"
        assert approve(quality, first)["outcome"] == "assessment_added"
        first_only = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert [row["id"] for row in first_only["candidates"]] == [first["id"]]
        assert first_only["candidates"][0]["valid_seated_event_id"] != original_seat
        clock.value += timedelta(minutes=6)
        transition(store, second, "seat")
        # Every clear is scoped to its observed global event/issue boundary.
        # The unrelated seating also requires 001's verifier to refresh it.
        assert review(quality, first, first_issues[0]["issue_id"])["outcome"] == "review_added"
        assert review(quality, second, second_issues[0]["issue_id"])["outcome"] == "review_added"
        rejected = approve(quality, second)
        assert rejected["outcome"] == "rejected" and rejected["response_status"] == 409
        partial = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert partial["candidates"] == []
        # The changed global event/review boundary conservatively invalidates
        # the earlier first-reception assessment as well as blocking partial 002.
        assert {row["reception_id"] for row in partial["excluded"]} == {first["id"], second["id"]}
        assert review(quality, second, second_issues[1]["issue_id"])["outcome"] == "review_added"
        assert approve(quality, first)["outcome"] == "assessment_added"
        assert approve(quality, second)["outcome"] == "assessment_added"
        approved = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert [row["id"] for row in approved["candidates"]] == [first["id"], second["id"]]
        assert [row["actual_wait_minutes"] for row in approved["candidates"]] == [25, 20.5]
        assert [row["reception_revision"] for row in approved["candidates"]] == [3, 1]
        assert approved["excluded"] == []
        with lab.connection(database, readonly=True) as connection:
            assert original == {table: list(connection.execute(f"SELECT * FROM {table}"))
                                for table in original}
        assert store.get(second["id"])["snapshot"]["waiting_groups"] == 0
        assert store.get_data_quality(first["id"])["training_use"] == "blocked"
        # A subsequent conservative decision immediately removes both rows
        # from this approved batch; no previous boolean flag can override it.
        assert review(quality, first, first_issues[0]["issue_id"], decision="hold")["outcome"] == "review_added"
        assert extract_candidates(database, authority=SyntheticAuthority(), clock=clock)["candidates"] == []


@pytest.mark.parametrize("decision", ["hold", "exclude"])
def test_latest_noneligible_assessment_prevents_an_older_approval_from_being_used(decision):
    with environment() as (_, database, store, clock):
        reception = register(store)
        transition(store, reception, "seat")
        quality = approved_store(store)
        assert approve(quality, reception)["outcome"] == "assessment_added"
        assert extract_candidates(database, authority=SyntheticAuthority())["candidates"]
        assert approve(quality, reception, decision=decision)["outcome"] == "assessment_added"
        result = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert result["candidates"] == [] and result["excluded"][0]["reasons"]


def test_new_correction_after_approval_invalidates_label_and_requires_new_quality_confirmation():
    with environment() as (_, database, store, clock):
        reception = register(store)
        transition(store, reception, "seat")
        quality = approved_store(store)
        assert approve(quality, reception)["outcome"] == "assessment_added"
        assert extract_candidates(database, authority=SyntheticAuthority())["candidates"]
        clock.value += timedelta(minutes=1)
        correction(store, reception)
        assert extract_candidates(database, authority=SyntheticAuthority())["candidates"] == []
        clock.value += timedelta(minutes=1)
        transition(store, reception, "seat")
        assert extract_candidates(database, authority=SyntheticAuthority())["candidates"] == []
        assert quality.context(reception["id"])["issues"]


def test_approved_boundary_gives_existing_preprocessing_only_accepted_rows():
    import pandas as pd

    from preprocessing import prepare_training_data

    with environment() as (_, database, store, clock):
        accepted, denied = register(store), register(store)
        clock.value += timedelta(minutes=25)
        transition(store, accepted, "seat")
        transition(store, denied, "seat")
        quality = approved_store(store)
        assert approve(quality, accepted)["outcome"] == "assessment_added"
        output = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert [row["id"] for row in output["candidates"]] == [accepted["id"]]
        # This test-only adapter handles one supported integer-minute/same-day
        # example. There is no production writer/adapter and no rounding of the
        # exact fractional/overnight contract proved by the other tests.
        rows = []
        for row in output["candidates"]:
            received = datetime.fromisoformat(row["received_at"])
            seated = datetime.fromisoformat(row["seated_at"])
            rows.append({"日付": received.date(), "呼び出し番号": row["display_number"],
                         "人数": row["total_people"], "大人": row["adults"], "子供": row["children"],
                         "受付時間": received.time(), "案内時刻": seated.time(),
                         "待ち時間": row["actual_wait_minutes"]})
        prepared = prepare_training_data(pd.DataFrame(rows))
        assert len(prepared.X) == 1 and list(prepared.y) == [25]
        assert output["excluded"][0]["reception_id"] == denied["id"]


def test_trusted_verifier_does_not_implicitly_assess_a_seated_reception():
    with environment() as (_, database, store, clock):
        reception = register(store)
        transition(store, reception, "seat")
        result = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert result["candidates"] == []
        assert "not_assessed" in result["excluded"][0]["reasons"]


def test_snapshot_keeps_previously_approved_label_separate_from_concurrent_correction():
    with environment() as (_, database, store, clock):
        reception = register(store)
        clock.value += timedelta(minutes=25)
        transition(store, reception, "seat")
        original_seat = store.get_events(reception["id"])[-1]["event_id"]
        quality = approved_store(store)
        assert approve(quality, reception)["outcome"] == "assessment_added"
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

        def correct_after_snapshot(stage):
            assert stage == "snapshot_acquired"
            clock.value += timedelta(minutes=1)
            assert correction(store, reception)["outcome"] == "state_changed"

        old_snapshot = extract_candidates(database, authority=SyntheticAuthority(), clock=clock,
                                          checkpoint=correct_after_snapshot)
        assert len(old_snapshot["candidates"]) == 1
        candidate = old_snapshot["candidates"][0]
        assert candidate["actual_wait_minutes"] == 25
        assert candidate["reception_revision"] == 1
        assert candidate["valid_seated_event_id"] == original_seat
        assert old_snapshot["snapshot"]["event_id"] == original_seat
        assert old_snapshot["snapshot"]["issue_id"] == 0
        new_snapshot = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert new_snapshot["candidates"] == []
        assert new_snapshot["snapshot"]["event_id"] > old_snapshot["snapshot"]["event_id"]
        assert new_snapshot["snapshot"]["issue_id"] == 1


@pytest.mark.parametrize("provider_state", ["revoked", "wrong_contract", "different_authority", "truthy", "error"])
def test_previously_approved_data_requires_current_verifier_permission(provider_state):
    class ChangedAuthority(SyntheticAuthority):
        def verify(self, *args):
            if provider_state == "revoked":
                return False
            if provider_state == "truthy":
                return 1
            if provider_state == "error":
                raise RuntimeError("Synthetic provider unavailable.")
            return super().verify(*args)

    with environment() as (_, database, store, clock):
        reception = register(store)
        transition(store, reception, "seat")
        quality = approved_store(store)
        assert approve(quality, reception)["outcome"] == "assessment_added"
        assert extract_candidates(database, authority=SyntheticAuthority())["candidates"]
        authority = ChangedAuthority()
        if provider_state == "wrong_contract":
            authority.dataset_contract = "unknown-future-feature-set"
        elif provider_state == "different_authority":
            authority.authority_id = "different-synthetic-authority"
        before = database.read_bytes()
        extracted = extract_candidates(database, authority=authority, clock=clock)
        assert extracted["candidates"] == []
        assert "unverified_assessment_authority" in extracted["excluded"][0]["reasons"]
        assert database.read_bytes() == before


def test_rotated_write_epoch_invalidates_previously_approved_data_without_rewriting_history():
    with environment() as (lab, database, store, clock):
        reception = register(store)
        transition(store, reception, "seat")
        quality = approved_store(store)
        assert approve(quality, reception)["outcome"] == "assessment_added"
        old = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert len(old["candidates"]) == 1
        new_epoch = str(uuid4())
        with lab.connection(database) as connection:
            connection.execute("UPDATE store_metadata SET write_epoch=?", (new_epoch,))
        before = database.read_bytes()
        extracted = extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert extracted["candidates"] == []
        assert extracted["snapshot"]["write_epoch"] == new_epoch
        assert extracted["snapshot"]["write_epoch"] != old["snapshot"]["write_epoch"]
        assert "unverified_assessment_authority" in extracted["excluded"][0]["reasons"]
        assert database.read_bytes() == before


def test_unknown_quality_policy_stops_extraction_even_when_archive_matches_the_row():
    """A future policy cannot become permission by updating only its version labels."""
    import json
    from staff_operation import canonical_json

    with environment() as (lab, database, store, clock):
        reception = register(store)
        transition(store, reception, "seat")
        accepted = approve(approved_store(store), reception)
        operation_id = accepted["operation_id"]
        with lab.connection(database) as connection:
            triggers = connection.execute("SELECT name,sql FROM sqlite_schema WHERE name IN "
                "('staff_operations_no_update','data_quality_assessments_no_update')").fetchall()
            encoded = connection.execute("SELECT request_json,response_json FROM staff_operations "
                                         "WHERE operation_id=?", (operation_id,)).fetchone()
            request, response = (json.loads(value) for value in encoded)
            request["command"]["policy_version"] = "unknown-future-policy"
            response["quality_result"]["policy_version"] = "unknown-future-policy"
            request_json = canonical_json(request)
            connection.execute("BEGIN IMMEDIATE")
            try:
                for name, _ in triggers:
                    connection.execute(f'DROP TRIGGER "{name}"')
                connection.execute("UPDATE data_quality_assessments SET policy_version=? WHERE operation_id=?",
                                   ("unknown-future-policy", operation_id))
                connection.execute("UPDATE staff_operations SET request_json=?,request_sha256=?,response_json=? "
                                   "WHERE operation_id=?", (request_json, sha256(request_json.encode()).hexdigest(),
                                                           canonical_json(response), operation_id))
                for _, ddl in triggers:
                    connection.execute(ddl)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        before = database.read_bytes()
        with pytest.raises(ExtractionValidationError):
            extract_candidates(database, authority=SyntheticAuthority(), clock=clock)
        assert database.read_bytes() == before
