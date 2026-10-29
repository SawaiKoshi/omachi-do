"""Replay quality decisions and fail closed on disposable history corruption."""

from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from quality_history import validate_quality_history
from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_schema import SchemaValidationError, validate_history, validate_schema
from reception_store import ReceptionStore, StoreValidationError
from staff_operation import canonical_json


ACTOR = {"principal_kind": "shared_staff_token", "principal_id": "quality-history-shared"}


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 30, 18, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


def predictor(**kwargs):
    return {"status": "available", "minutes": 12.125, "model_version": "synthetic-only",
            "predicted_at": kwargs["received_at"].isoformat(), "warnings": []}


@contextmanager
def quality_history():
    from quality_review_store import QualityReviewStore

    with temporary_workspace() as lab:
        database, _ = create_v1(lab, "quality-history.sqlite3", populated=False)
        migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        with lab.connection(database) as conn:
            conn.execute("UPDATE store_metadata SET staff_write_mode='revision_required',seat_corrections_enabled=1")
        clock = Clock()
        store = ReceptionStore(database, clock=clock)
        first, _ = store.register(2, 0, str(uuid4()), predictor=predictor)

        def arguments():
            context = store.get_staff_reception(first["id"])
            return dict(operation_id=str(uuid4()), expected_revision=context["revision"],
                        write_epoch=context["write_epoch"], **ACTOR)

        clock.value += timedelta(minutes=10)
        store.staff_transition(first["id"], "seat", **arguments())
        target = store.get_events(first["id"])[-1]["event_id"]
        clock.value += timedelta(seconds=30)
        second, _ = store.register(3, 0, str(uuid4()), predictor=predictor)
        clock.value += timedelta(seconds=30)
        store.correct_seating(first["id"], target_event_id=target, reason_code="accidental_seating",
                              reason_detail="Synthetic incorrect seating.", **arguments())
        clock.value = clock.value.replace(minute=25)
        store.staff_transition(first["id"], "seat", **arguments())
        quality = QualityReviewStore(store)
        context = quality.context(first["id"])
        common = dict(operation_id=str(uuid4()), expected_revision=context["revision"],
                      write_epoch=context["write_epoch"], expected_watermarks=context["watermarks"],
                      decision="hold", reason_code="pending_evidence", reason_detail="Await independent evidence.",
                      evidence={}, **ACTOR)
        review = quality.review_issue(first["id"], issue_id=context["issues"][0]["issue_id"],
                                      expected_review_number=0, **common)
        assert review["response_status"] == 200
        context = quality.context(first["id"])
        common.update(operation_id=str(uuid4()), expected_watermarks=context["watermarks"])
        assessment = quality.assess(first["id"], expected_assessment_id=0, **common)
        assert assessment["response_status"] == 200
        with lab.connection(database, readonly=True) as conn:
            validate_schema(conn, 2, allow_revision_required=True)
            validate_history(conn, 2)
        yield lab, database, clock, first, second, review, assessment


def _row(conn, table, column, value):
    cursor = conn.execute(f'SELECT * FROM "{table}" WHERE "{column}"=?', (value,))
    return dict(zip([item[0] for item in cursor.description], cursor.fetchone()))


def damage(lab, database, mutator):
    with lab.connection(database) as conn:
        names = ("data_quality_reviews", "data_quality_assessments", "staff_operations")
        triggers = [(row[0], row[1]) for row in conn.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger'")
            if any(row[0].startswith(name + "_no_") for name in names)]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name, _ in triggers:
                conn.execute(f'DROP TRIGGER "{name}"')
            mutator(conn)
            for _, ddl in triggers:
                conn.execute(ddl)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        validate_schema(conn, 2, allow_revision_required=True)


def set_command(conn, operation_id, mutate):
    operation = _row(conn, "staff_operations", "operation_id", operation_id)
    request = json.loads(operation["request_json"])
    mutate(request["command"])
    encoded = canonical_json(request)
    conn.execute("UPDATE staff_operations SET request_json=?,request_sha256=? WHERE operation_id=?",
                 (encoded, sha256(encoded.encode()).hexdigest(), operation_id))


def test_unresolved_quality_decisions_survive_reopen_without_altering_the_wait_label():
    with quality_history() as (lab, database, clock, first, second, review, assessment):
        reopened = ReceptionStore(database, clock=clock)
        assert reopened.get(first["id"])["actual_wait_minutes"] == 25
        assert reopened.get(second["id"]) == second
        with lab.connection(database, readonly=True) as conn:
            validate_quality_history(conn)
            assert conn.execute("SELECT decision FROM data_quality_reviews").fetchone()[0] == "hold"
            assert conn.execute("SELECT decision FROM data_quality_assessments").fetchone()[0] == "hold"


@pytest.mark.parametrize("case", [
    "review_reason", "review_number", "review_request_reason", "review_self_watermark",
    "review_omits_correction", "assessment_self_predecessor", "assessment_watermarks",
    "assessment_revision", "assessment_seating", "missing_review", "missing_assessment",
    "review_sequence", "assessment_sequence",
])
def test_damaged_quality_history_is_rejected_without_writing(case):
    with quality_history() as (lab, database, clock, first, second, review, assessment):
        review_id = review["quality_result"]["review_id"]
        assessment_id = assessment["quality_result"]["assessment_id"]

        def mutate(conn):
            if case == "review_reason":
                conn.execute("UPDATE data_quality_reviews SET reason_detail='Changed reason' WHERE review_id=?", (review_id,))
            elif case == "review_number":
                conn.execute("UPDATE data_quality_reviews SET review_number=2 WHERE review_id=?", (review_id,))
            elif case == "review_request_reason":
                set_command(conn, review["operation_id"], lambda command: command.update(reason_detail="Other reason"))
            elif case == "review_self_watermark":
                set_command(conn, review["operation_id"], lambda command: command["expected_watermarks"].update(review_id=review_id))
            elif case == "review_omits_correction":
                set_command(conn, review["operation_id"], lambda command: command["expected_watermarks"].update(event_id=0, issue_id=0))
            elif case == "assessment_self_predecessor":
                set_command(conn, assessment["operation_id"], lambda command: command.update(expected_assessment_id=assessment_id))
            elif case == "assessment_watermarks":
                conn.execute("UPDATE data_quality_assessments SET reviews_through_id=0 WHERE assessment_id=?", (assessment_id,))
            elif case == "assessment_revision":
                conn.execute("UPDATE data_quality_assessments SET reception_revision=0 WHERE assessment_id=?", (assessment_id,))
            elif case == "assessment_seating":
                conn.execute("UPDATE data_quality_assessments SET valid_seated_event_id=NULL WHERE assessment_id=?", (assessment_id,))
            elif case == "missing_review":
                # Remove the dependent hold assessment first, retaining both
                # successful ledger claims for the reverse-link validation.
                conn.execute("DELETE FROM data_quality_assessments WHERE assessment_id=?", (assessment_id,))
                conn.execute("DELETE FROM data_quality_reviews WHERE review_id=?", (review_id,))
            elif case == "missing_assessment":
                conn.execute("DELETE FROM data_quality_assessments WHERE assessment_id=?", (assessment_id,))
            elif case in ("review_sequence", "assessment_sequence"):
                table = "data_quality_reviews" if case == "review_sequence" else "data_quality_assessments"
                conn.execute("UPDATE sqlite_sequence SET seq=0 WHERE name=?", (table,))
            else:
                raise AssertionError(case)

        damage(lab, database, mutate)
        unchanged = database.read_bytes()
        with lab.connection(database, readonly=True) as conn:
            with pytest.raises(SchemaValidationError):
                validate_quality_history(conn)
        with pytest.raises(StoreValidationError):
            ReceptionStore(database, clock=clock)
        assert database.read_bytes() == unchanged


class SyntheticAuthority:
    authority_id = "synthetic-history-verifier"
    dataset_contract = "rf-intake-actual-wait-v1"

    def verify(self, kind, principal_id, permission, evidence, context):
        return (kind == "staff_account" and principal_id == "synthetic-reviewer"
                and permission in ("clear", "eligible")
                and evidence["reference"] == "synthetic-confirmed-log")


def verified_decisions(database, clock, first):
    from quality_review_store import QualityReviewStore

    store = ReceptionStore(database, clock=clock)
    quality = QualityReviewStore(store, authority=SyntheticAuthority())
    actor = {"principal_kind": "staff_account", "principal_id": "synthetic-reviewer"}
    evidence = {"reference": "synthetic-confirmed-log", "intake_verified": True,
                "seating_verified": True, "context_understood": True}
    context = quality.context(first["id"])
    issue = context["issues"][0]
    common = dict(operation_id=str(uuid4()), expected_revision=context["revision"],
                  write_epoch=context["write_epoch"], expected_watermarks=context["watermarks"],
                  decision="clear", reason_code="confirmed_actual_log", reason_detail="Synthetic independently checked log.",
                  evidence=evidence, **actor)
    review = quality.review_issue(first["id"], issue_id=issue["issue_id"],
                                  expected_review_number=issue["latest_review"]["review_number"], **common)
    assert review["response_status"] == 200
    context = quality.context(first["id"])
    common.update(operation_id=str(uuid4()), expected_watermarks=context["watermarks"], decision="eligible")
    assessment = quality.assess(first["id"], expected_assessment_id=context["latest_assessment_id"], **common)
    assert assessment["response_status"] == 200
    return store, review, assessment


@pytest.mark.parametrize("later", ["registration", "correction"])
def test_later_context_does_not_rewrite_or_invalidate_a_sound_historical_approval(later):
    with quality_history() as (lab, database, clock, first, second, *_):
        store, review, assessment = verified_decisions(database, clock, first)
        with lab.connection(database, readonly=True) as conn:
            validate_history(conn, 2)
            archived = conn.execute("SELECT request_json,response_json FROM staff_operations WHERE operation_id=?",
                                    (assessment["operation_id"],)).fetchone()
        clock.value += timedelta(minutes=1)
        if later == "registration":
            store.register(1, 0, str(uuid4()), predictor=predictor)
        else:
            context = store.get_staff_reception(first["id"])
            store.correct_seating(first["id"], operation_id=str(uuid4()), expected_revision=context["revision"],
                                  write_epoch=context["write_epoch"], target_event_id=assessment["quality_result"]["valid_seated_event_id"],
                                  reason_code="accidental_seating", reason_detail="Second synthetic correction.", **ACTOR)
        with lab.connection(database, readonly=True) as conn:
            validate_history(conn, 2)
            assert conn.execute("SELECT request_json,response_json FROM staff_operations WHERE operation_id=?",
                                (assessment["operation_id"],)).fetchone() == archived
            from quality_policy import candidate_reasons
            assert candidate_reasons(conn, first["id"], authority=SyntheticAuthority())
        ReceptionStore(database, clock=clock)


@pytest.mark.parametrize("target", ["clear_review", "eligible_assessment"])
@pytest.mark.parametrize("change", ["seating", "revision_type"])
def test_release_authorization_must_reference_the_exact_verified_seating(target, change):
    with quality_history() as (lab, database, clock, first, second, *_):
        _, review, assessment = verified_decisions(database, clock, first)
        operation_id = review["operation_id"] if target == "clear_review" else assessment["operation_id"]
        with lab.connection(database, readonly=True) as conn:
            invalid_seat = conn.execute("SELECT event_id FROM reception_events WHERE reception_id=? "
                                       "AND event_type='seated' ORDER BY event_id LIMIT 1", (first["id"],)).fetchone()[0]

        def mutate(conn):
            operation = _row(conn, "staff_operations", "operation_id", operation_id)
            wrapper = json.loads(operation["request_json"])
            if change == "seating":
                wrapper["authorization"]["valid_seated_event_id"] = invalid_seat
            else:
                # Python numeric equality alone would treat 3.0 as revision 3;
                # canonical audit authorization must preserve integer type.
                wrapper["authorization"]["reception_revision"] = float(wrapper["authorization"]["reception_revision"])
            encoded = canonical_json(wrapper)
            conn.execute("UPDATE staff_operations SET request_json=?,request_sha256=? WHERE operation_id=?",
                         (encoded, sha256(encoded.encode()).hexdigest(), operation_id))

        damage(lab, database, mutate)
        unchanged = database.read_bytes()
        with lab.connection(database, readonly=True) as conn:
            with pytest.raises(SchemaValidationError):
                validate_quality_history(conn)
        with pytest.raises(StoreValidationError):
            ReceptionStore(database, clock=clock)
        assert database.read_bytes() == unchanged


@pytest.mark.parametrize("child", ["review", "assessment"])
def test_live_replay_and_result_lookup_cannot_hide_a_missing_quality_child(child):
    from quality_review_store import QualityReviewStore

    with quality_history() as (lab, database, clock, first, second, review, assessment):
        store = ReceptionStore(database, clock=clock)
        quality = QualityReviewStore(store)
        result = review if child == "review" else assessment
        with lab.connection(database, readonly=True) as conn:
            operation = _row(conn, "staff_operations", "operation_id", result["operation_id"])
        command = json.loads(operation["request_json"])["command"]
        arguments = {name: value for name, value in command.items()
                     if name not in ("request_schema_version", "reception_id", "action", "policy_version")}
        arguments.update(operation_id=result["operation_id"], **ACTOR)

        def remove_child(conn):
            conn.execute("DELETE FROM data_quality_assessments WHERE assessment_id=?",
                         (assessment["quality_result"]["assessment_id"],))
            if child == "review":
                conn.execute("DELETE FROM data_quality_reviews WHERE review_id=?",
                             (review["quality_result"]["review_id"],))

        damage(lab, database, remove_child)
        unchanged = database.read_bytes()
        retry = quality.review_issue if child == "review" else quality.assess
        with pytest.raises(StoreValidationError):
            retry(first["id"], **arguments)
        assert database.read_bytes() == unchanged
        with pytest.raises(StoreValidationError):
            store.get_staff_operation(result["operation_id"], **ACTOR)
        assert database.read_bytes() == unchanged


def test_quality_history_cannot_be_reclassified_as_legacy_mode():
    with quality_history() as (lab, database, clock, *_):
        with lab.connection(database) as conn:
            trigger = conn.execute("SELECT sql FROM sqlite_schema WHERE name='metadata_no_staff_downgrade'").fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DROP TRIGGER metadata_no_staff_downgrade")
                conn.execute("UPDATE store_metadata SET seat_corrections_enabled=0,staff_write_mode='legacy'")
                conn.execute(trigger)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            validate_schema(conn, 2, allow_revision_required=True)
        unchanged = database.read_bytes()
        with lab.connection(database, readonly=True) as conn:
            with pytest.raises(SchemaValidationError, match="legacy staff mode"):
                validate_quality_history(conn)
        with pytest.raises(StoreValidationError):
            ReceptionStore(database, clock=clock)
        assert database.read_bytes() == unchanged
