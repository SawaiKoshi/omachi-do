"""Quality decisions are audited, conservative and synthetic-database only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
import sqlite3
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from quality_review_store import QualityReviewStore
from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_store import ReceptionNotFound, ReceptionStore, StateConflict, StoreValidationError


SHARED = {"principal_kind": "shared_staff_token", "principal_id": "shared-staff"}
VERIFIED = {"principal_kind": "staff_account", "principal_id": "verified-synthetic-reviewer"}
INTAKE_TABLES = ("reception_snapshots", "predictions", "idempotency_keys", "daily_counters")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 30, 18, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


class SyntheticAuthority:
    """Test-only attestation; the production shared token never enables this."""

    authority_id = "synthetic-verifier"
    dataset_contract = "rf-intake-actual-wait-v1"

    def verify(self, principal_kind, principal_id, permission, evidence, context):
        return (dict(principal_kind=principal_kind, principal_id=principal_id) == VERIFIED
                and evidence.get("reference") == "synthetic/source-records-001"
                and all(evidence.get(field) is True for field in
                        ("intake_verified", "seating_verified", "context_understood")))


def evidence():
    return dict(reference="synthetic/source-records-001", intake_verified=True,
                seating_verified=True, context_understood=True)


def prediction(**kwargs):
    return dict(status="available", minutes=12.125, model_version="synthetic-only",
                predicted_at=kwargs["received_at"].isoformat(), warnings=[])


@contextmanager
def synthetic_store(*, version=2, mode="revision_required", enabled=1):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, "quality-review.sqlite3", populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
            with lab.connection(database) as conn:
                conn.execute("UPDATE store_metadata SET staff_write_mode=?,seat_corrections_enabled=?",
                             (mode, enabled))
        clock = Clock()
        yield lab, database, ReceptionStore(database, clock=clock), clock


@pytest.fixture
def protected():
    with synthetic_store() as context:
        yield context


def register(store):
    row, created = store.register(2, 0, str(uuid4()), predictor=prediction)
    assert created
    return row


def staff_args(store, row):
    current = store.get_staff_reception(row["id"])
    return dict(operation_id=str(uuid4()), expected_revision=current["revision"],
                write_epoch=current["write_epoch"], **SHARED)


def transition(store, row, action):
    return store.staff_transition(row["id"], action, **staff_args(store, row))


def correct(store, row):
    target = next(event for event in reversed(store.get_events(row["id"]))
                  if event["event_type"] == "seated")
    return store.correct_seating(row["id"], target_event_id=target["event_id"],
                                 reason_code="accidental_seating", reason_detail="合成誤案内の訂正",
                                 **staff_args(store, row))


def scenario(store, clock, *, reseat=True):
    first = register(store)
    clock.value += timedelta(minutes=10)
    transition(store, first, "seat")
    clock.value += timedelta(seconds=30)
    second = register(store)
    clock.value += timedelta(seconds=30)
    correct(store, first)
    if reseat:
        clock.value += timedelta(minutes=14)
        transition(store, first, "seat")
    return first, second


def table(lab, database, name):
    with lab.connection(database, readonly=True) as conn:
        cursor = conn.execute(f"SELECT * FROM {name}")
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor]


def immutable_intake(lab, database):
    return {name: table(lab, database, name) for name in INTAKE_TABLES}


def review_args(quality, row, issue_id, decision="hold", *, actor=SHARED, **overrides):
    context = quality.context(row["id"])
    issue = next(issue for issue in context["issues"] if issue["issue_id"] == issue_id)
    previous = issue["latest_review"]
    args = dict(operation_id=str(uuid4()), expected_revision=context["revision"],
                write_epoch=context["write_epoch"], issue_id=issue_id,
                expected_review_number=previous["review_number"] if previous else 0,
                expected_watermarks=context["watermarks"], decision=decision,
                reason_code="synthetic_verification", reason_detail="合成記録を根拠に品質を確認",
                evidence=evidence() if decision == "clear" else {}, **actor)
    args.update(overrides)
    return args


def review(quality, row, issue_id, decision="hold", **overrides):
    return quality.review_issue(row["id"], **review_args(quality, row, issue_id, decision, **overrides))


def assessment_args(quality, row, decision="hold", *, actor=SHARED, **overrides):
    context = quality.context(row["id"])
    args = dict(operation_id=str(uuid4()), expected_revision=context["revision"],
                write_epoch=context["write_epoch"],
                expected_assessment_id=context["latest_assessment_id"],
                expected_watermarks=context["watermarks"], decision=decision,
                reason_code="synthetic_verification", reason_detail="合成記録を根拠に採否を確認",
                evidence=evidence() if decision == "eligible" else {}, **actor)
    args.update(overrides)
    return args


def assess(quality, row, decision="hold", **overrides):
    return quality.assess(row["id"], **assessment_args(quality, row, decision, **overrides))


def assert_reopened(database, clock, row, *, expected_wait=25):
    reopened = ReceptionStore(database, clock=clock)
    assert reopened.get(row["id"])["actual_wait_minutes"] == expected_wait
    return reopened


def test_required_001_002_context_preserves_intake_and_blocks_unreviewed_and_partial_release(protected):
    lab, database, store, clock = protected
    first, second = scenario(store, clock)
    quality = QualityReviewStore(store, authority=SyntheticAuthority())
    before = immutable_intake(lab, database)
    old_events = {row["id"]: store.get_events(row["id"]) for row in (first, second)}
    first_context, second_context = quality.context(first["id"]), quality.context(second["id"])
    assert store.get(first["id"])["actual_wait_minutes"] == 25
    assert {issue["issue_kind"] for issue in first_context["issues"]} == {"invalidated_seating"}
    assert {issue["issue_kind"] for issue in second_context["issues"]} == {"snapshot_context", "prediction_gate"}
    assert all(issue["latest_review"] is None for context in (first_context, second_context)
               for issue in context["issues"])
    assert first_context["latest_assessment_id"] == 0
    assert second_context["valid_seated_event_id"] is None
    for row in (first, second):
        assert store.get_data_quality(row["id"])["training_use"] == "blocked"
        rejected = assess(quality, row, "eligible", actor=VERIFIED)
        assert rejected["outcome"] == "rejected" and rejected["response_status"] in {403, 409}
    # A correct seating label does not erase the source issue or grant approval.
    issue = first_context["issues"][0]
    cleared = review(quality, first, issue["issue_id"], "clear", actor=VERIFIED)
    assert cleared["outcome"] == "review_added"
    assert cleared["quality_result"]["decision"] == "clear"
    eligible = assess(quality, first, "eligible", actor=VERIFIED)
    assert eligible["outcome"] == "assessment_added"
    assert eligible["quality_result"]["valid_seated_event_id"] == first_context["valid_seated_event_id"]
    assert store.get_data_quality(first["id"])["training_use"] == "blocked"
    # The other receipt must first acquire a valid label, then both issues clear.
    waiting_clear = review(quality, second, second_context["issues"][0]["issue_id"], "clear", actor=VERIFIED)
    assert waiting_clear["outcome"] == "rejected"
    clock.value += timedelta(minutes=5)
    transition(store, second, "seat")
    review(quality, second, second_context["issues"][0]["issue_id"], "clear", actor=VERIFIED)
    partial = assess(quality, second, "eligible", actor=VERIFIED)
    assert partial["outcome"] == "rejected" and partial["response_status"] == 409
    review(quality, second, second_context["issues"][1]["issue_id"], "clear", actor=VERIFIED)
    assert assess(quality, second, "eligible", actor=VERIFIED)["outcome"] == "assessment_added"
    assert immutable_intake(lab, database) == before
    assert store.get_events(first["id"]) == old_events[first["id"]]
    assert store.get_events(second["id"])[:-1] == old_events[second["id"]]
    assert len(table(lab, database, "data_quality_issues")) == 3
    assert_reopened(database, clock, first)


@pytest.mark.parametrize("decision", ["hold", "exclude"])
def test_shared_principal_can_append_conservative_decisions_but_not_release(protected, decision):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    before = store.get_staff_reception(first["id"])
    for number in (1, 2):
        result = review(quality, first, issue_id, decision)
        assert result["outcome"] == "review_added"
        assert result["quality_result"]["review_number"] == number
        assert result["quality_result"]["decision"] == decision
        assert result["resulting_revision"] == before["revision"]
    assessment = assess(quality, first, decision)
    assert assessment["outcome"] == "assessment_added"
    assert assessment["quality_result"]["decision"] == decision
    assert store.get_staff_reception(first["id"]) == before
    assert store.get_data_quality(first["id"])["training_use"] == "blocked"
    operations = table(lab, database, "staff_operations")[-3:]
    assert all(operation["principal_kind"] == SHARED["principal_kind"]
               and operation["principal_id"] == SHARED["principal_id"] for operation in operations)
    assert_reopened(database, clock, first)


@pytest.mark.parametrize("authority", [None, SyntheticAuthority()])
@pytest.mark.parametrize("operation", ["clear", "eligible"])
def test_shared_identity_never_grants_release_even_with_injected_authority(protected, authority, operation):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store, authority=authority)
    result = (review(quality, first, quality.context(first["id"])["issues"][0]["issue_id"], "clear")
              if operation == "clear" else assess(quality, first, "eligible"))
    assert result["outcome"] == "rejected" and result["response_status"] == 403
    assert not table(lab, database, "data_quality_reviews")
    assert not table(lab, database, "data_quality_assessments")
    assert store.get_staff_operation(result["operation_id"], **SHARED) == result
    assert_reopened(database, clock, first)


@pytest.mark.parametrize("operation", ["clear", "eligible"])
def test_unconfigured_authority_denies_claimed_account_identity(protected, operation):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    result = (review(quality, first, quality.context(first["id"])["issues"][0]["issue_id"],
                     "clear", actor=VERIFIED)
              if operation == "clear" else assess(quality, first, "eligible", actor=VERIFIED))
    assert result["outcome"] == "rejected" and result["response_status"] == 403
    assert not table(lab, database, "data_quality_reviews")
    assert not table(lab, database, "data_quality_assessments")


@pytest.mark.parametrize("answer", [False, None, 1, "true"])
def test_authority_must_return_literal_true_for_release(protected, answer):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)

    class UntrustedAuthority(SyntheticAuthority):
        def verify(self, *args):
            return answer

    quality = QualityReviewStore(store, authority=UntrustedAuthority())
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    result = review(quality, first, issue_id, "clear", actor=VERIFIED)
    assert result["outcome"] == "rejected" and result["response_status"] == 403
    assert not table(lab, database, "data_quality_reviews")


def test_external_evidence_reference_is_verified_and_not_a_self_asserted_checkbox(protected):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store, authority=SyntheticAuthority())
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    wrong_reference = evidence() | {"reference": "self-asserted-with-no-source-record"}
    result = review(quality, first, issue_id, "clear", actor=VERIFIED, evidence=wrong_reference)
    assert result["outcome"] == "rejected" and result["response_status"] == 403
    assert not table(lab, database, "data_quality_reviews")


@pytest.mark.parametrize("field,value", [("reference", ""), ("reference", " " * 5),
                                         ("reference", "x" * 129), ("intake_verified", False),
                                         ("seating_verified", False), ("context_understood", False),
                                         ("intake_verified", 1)])
def test_release_requires_complete_well_formed_evidence_without_writing(protected, field, value):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store, authority=SyntheticAuthority())
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    args = review_args(quality, first, issue_id, "clear", actor=VERIFIED,
                       evidence=evidence() | {field: value})
    before = database.read_bytes()
    with pytest.raises(StoreValidationError):
        quality.review_issue(first["id"], **args)
    assert database.read_bytes() == before


def test_lost_review_response_replays_exactly_after_new_reviews_and_reopen(protected):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    args = review_args(quality, first, issue_id)
    original = quality.review_issue(first["id"], **args)
    review(quality, first, issue_id, "exclude")
    restarted = assert_reopened(database, clock, first)
    replay = QualityReviewStore(restarted)
    before = database.read_bytes()
    for _ in range(3):
        assert replay.review_issue(first["id"], **args) == original
        assert restarted.get_staff_operation(args["operation_id"], **SHARED) == original
        assert database.read_bytes() == before
    assert len(table(lab, database, "data_quality_reviews")) == 2


def test_lost_assessment_response_replays_exactly_without_second_assessment(protected):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    args = assessment_args(quality, first, "exclude")
    original = quality.assess(first["id"], **args)
    restarted = assert_reopened(database, clock, first)
    replay = QualityReviewStore(restarted)
    before = database.read_bytes()
    assert replay.assess(first["id"], **args) == original
    assert restarted.get_staff_operation(args["operation_id"], **SHARED) == original
    assert database.read_bytes() == before
    assert len(table(lab, database, "data_quality_assessments")) == 1


@pytest.mark.parametrize("changed", ["decision", "reason", "evidence", "principal", "issue", "revision", "epoch"])
def test_operation_id_reuse_with_different_command_or_actor_never_writes(protected, changed):
    lab, database, store, clock = protected
    first, second = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    args = review_args(quality, first, issue_id)
    original = quality.review_issue(first["id"], **args)
    if changed == "decision":
        args["decision"] = "exclude"
    elif changed == "reason":
        args["reason_detail"] = "別の確認理由"
    elif changed == "evidence":
        args["evidence"] = evidence()
    elif changed == "principal":
        args["principal_id"] = "other-shared-actor"
    elif changed == "issue":
        args["issue_id"] = quality.context(second["id"])["issues"][0]["issue_id"]
    elif changed == "revision":
        args["expected_revision"] += 1
    else:
        args["write_epoch"] = str(uuid4())
    before = database.read_bytes()
    with pytest.raises(StateConflict):
        quality.review_issue(first["id"], **args)
    assert database.read_bytes() == before
    assert store.get_staff_operation(original["operation_id"], **SHARED) == original


@pytest.mark.parametrize("kind", ["review", "assessment"])
def test_stale_reception_revision_is_archived_rejection_without_quality_change(protected, kind):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock, reseat=False)
    quality = QualityReviewStore(store)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    args = review_args(quality, first, issue_id) if kind == "review" else assessment_args(quality, first)
    transition(store, first, "seat")
    current = store.get_staff_reception(first["id"])
    result = (quality.review_issue(first["id"], **args) if kind == "review"
              else quality.assess(first["id"], **args))
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert not table(lab, database, "data_quality_reviews")
    assert not table(lab, database, "data_quality_assessments")
    assert store.get_staff_reception(first["id"]) == current
    assert_reopened(database, clock, first, expected_wait=11)


@pytest.mark.parametrize("kind", ["review", "assessment"])
def test_wrong_epoch_is_archived_rejection_without_quality_change(protected, kind):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    args = (review_args(quality, first, issue_id, write_epoch=str(uuid4())) if kind == "review"
            else assessment_args(quality, first, write_epoch=str(uuid4())))
    method = quality.review_issue if kind == "review" else quality.assess
    result = method(first["id"], **args)
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert not table(lab, database, "data_quality_reviews")
    assert not table(lab, database, "data_quality_assessments")


def test_review_rejects_issue_owned_by_another_reception(protected):
    lab, database, store, clock = protected
    first, second = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue = quality.context(first["id"])["issues"][0]["issue_id"]
    other_issue = quality.context(second["id"])["issues"][0]["issue_id"]
    args = review_args(quality, first, issue)
    args["issue_id"] = other_issue
    result = quality.review_issue(first["id"], **args)
    assert result["outcome"] == "rejected" and result["response_status"] == 404
    assert not table(lab, database, "data_quality_reviews")


def test_global_watermarks_reject_review_if_another_receipt_was_updated(protected):
    lab, database, store, clock = protected
    first, second = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue = quality.context(first["id"])["issues"][0]["issue_id"]
    args = review_args(quality, first, issue)
    original_revision = store.get_staff_reception(first["id"])["revision"]
    transition(store, second, "call")
    result = quality.review_issue(first["id"], **args)
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert store.get_staff_reception(first["id"])["revision"] == original_revision
    assert not table(lab, database, "data_quality_reviews")


@pytest.mark.parametrize("kind", ["review", "assessment"])
def test_competing_quality_commands_cannot_both_commit_stale_quality_version(protected, kind):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    qualities = [QualityReviewStore(ReceptionStore(database, clock=clock)) for _ in range(2)]
    issue_id = qualities[0].context(first["id"])["issues"][0]["issue_id"]
    requests = [(review_args(quality, first, issue_id) if kind == "review"
                 else assessment_args(quality, first)) for quality in qualities]
    barrier = Barrier(2)

    def worker(item):
        quality, args = item
        barrier.wait(timeout=10)
        method = quality.review_issue if kind == "review" else quality.assess
        return method(first["id"], **args)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, zip(qualities, requests)))
    outcome = "review_added" if kind == "review" else "assessment_added"
    assert sorted(result["outcome"] for result in results) == sorted([outcome, "rejected"])
    assert next(result for result in results if result["outcome"] == "rejected")["response_status"] == 409
    name = "data_quality_reviews" if kind == "review" else "data_quality_assessments"
    assert len(table(lab, database, name)) == 1
    assert_reopened(database, clock, first)


def test_identical_concurrent_review_retries_append_only_once(protected):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    qualities = [QualityReviewStore(ReceptionStore(database, clock=clock)) for _ in range(3)]
    issue_id = qualities[0].context(first["id"])["issues"][0]["issue_id"]
    args = review_args(qualities[0], first, issue_id)
    barrier = Barrier(3)

    def worker(quality):
        barrier.wait(timeout=10)
        return quality.review_issue(first["id"], **args)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(worker, qualities))
    assert all(result == results[0] for result in results)
    assert results[0]["outcome"] == "review_added"
    assert len(table(lab, database, "data_quality_reviews")) == 1
    assert_reopened(database, clock, first)


def test_reviews_of_different_issues_still_reject_a_shared_stale_quality_snapshot(protected):
    lab, database, store, clock = protected
    _, second = scenario(store, clock)
    qualities = [QualityReviewStore(ReceptionStore(database, clock=clock)) for _ in range(2)]
    issue_ids = [issue["issue_id"] for issue in qualities[0].context(second["id"])["issues"]]
    requests = [review_args(quality, second, issue_id) for quality, issue_id in zip(qualities, issue_ids)]
    barrier = Barrier(2)

    def worker(item):
        quality, args = item
        barrier.wait(timeout=10)
        return quality.review_issue(second["id"], **args)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, zip(qualities, requests)))
    assert sorted(result["outcome"] for result in results) == ["rejected", "review_added"]
    assert len(table(lab, database, "data_quality_reviews")) == 1


def test_operation_identity_is_global_across_review_assessment_and_staff_transition(protected):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    args = review_args(quality, first, issue_id)
    original = quality.review_issue(first["id"], **args)
    before = database.read_bytes()
    with pytest.raises(StateConflict):
        quality.assess(first["id"], **assessment_args(quality, first, operation_id=args["operation_id"]))
    assert database.read_bytes() == before
    with pytest.raises(StateConflict):
        store.staff_transition(first["id"], "seat", operation_id=args["operation_id"],
                               expected_revision=args["expected_revision"], write_epoch=args["write_epoch"], **SHARED)
    assert database.read_bytes() == before
    assert store.get_staff_operation(args["operation_id"], **SHARED) == original


@pytest.mark.parametrize("kind", ["review", "assessment"])
@pytest.mark.parametrize("stage", ["after_quality", "after_ledger", "before_commit"])
def test_exception_rolls_back_quality_ledger_clock_and_sequences(protected, monkeypatch, kind, stage):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    args = review_args(quality, first, issue_id) if kind == "review" else assessment_args(quality, first)
    method = quality.review_issue if kind == "review" else quality.assess
    before = database.read_bytes()
    if stage == "after_quality":
        name = "_save_review" if kind == "review" else "_save_assessment"
        original = getattr(quality, name)

        def broken(*items, **kwargs):
            original(*items, **kwargs)
            raise RuntimeError("injected after quality")

        monkeypatch.setattr(quality, name, broken)
    elif stage == "after_ledger":
        original = store._save_staff_operation

        def broken(*items, **kwargs):
            original(*items, **kwargs)
            raise RuntimeError("injected after ledger")

        monkeypatch.setattr(store, "_save_staff_operation", broken)
    else:
        original = store._connection

        @contextmanager
        def broken(*, write=False):
            with original(write=write) as conn:
                yield conn
                if write:
                    raise RuntimeError("injected before commit")

        monkeypatch.setattr(store, "_connection", broken)
    with pytest.raises(RuntimeError, match="injected"):
        method(first["id"], **args)
    assert database.read_bytes() == before
    monkeypatch.undo()
    assert not table(lab, database, "data_quality_reviews")
    assert not table(lab, database, "data_quality_assessments")
    result = method(first["id"], **args)
    assert result["outcome"] == ("review_added" if kind == "review" else "assessment_added")
    assert_reopened(database, clock, first)


def test_new_correction_preserves_old_reviews_but_requires_review_of_every_new_issue(protected):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store, authority=SyntheticAuthority())
    original_issue = quality.context(first["id"])["issues"][0]["issue_id"]
    original_review = review(quality, first, original_issue, "clear", actor=VERIFIED)
    original_assessment = assess(quality, first, "eligible", actor=VERIFIED)
    correct(store, first)
    clock.value += timedelta(minutes=1)
    transition(store, first, "seat")
    context = quality.context(first["id"])
    assert len(context["issues"]) == 2
    assert context["revision"] > original_assessment["quality_result"]["reception_revision"]
    assert table(lab, database, "data_quality_reviews")[0] == original_review["quality_result"]
    result = assess(quality, first, "eligible", actor=VERIFIED)
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert any(issue["latest_review"] is None for issue in context["issues"])
    assert len({issue["issue_id"] for issue in context["issues"]}) == 2
    assert store.get_data_quality(first["id"])["training_use"] == "blocked"
    # Clearing the new issue alone does not renew the old revision's clearance.
    new_issue = next(issue for issue in context["issues"] if issue["issue_id"] != original_issue)
    review(quality, first, new_issue["issue_id"], "clear", actor=VERIFIED)
    stale_old_clearance = assess(quality, first, "eligible", actor=VERIFIED)
    assert stale_old_clearance["outcome"] == "rejected"
    review(quality, first, original_issue, "clear", actor=VERIFIED)
    assert assess(quality, first, "eligible", actor=VERIFIED)["outcome"] == "assessment_added"


def test_assessment_cannot_reuse_watermarks_from_before_a_new_review(protected):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    args = assessment_args(quality, first)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    review(quality, first, issue_id, "hold")
    result = quality.assess(first["id"], **args)
    assert result["outcome"] == "rejected" and result["response_status"] == 409
    assert not table(lab, database, "data_quality_assessments")


@pytest.mark.parametrize("table_name,id_name", [("data_quality_reviews", "review_id"),
                                               ("data_quality_assessments", "assessment_id")])
@pytest.mark.parametrize("mutation", ["update", "delete", "replace"])
def test_quality_decision_history_is_append_only(protected, table_name, id_name, mutation):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    review(quality, first, issue_id, "hold")
    assess(quality, first, "hold")
    before = table(lab, database, table_name)
    with pytest.raises(sqlite3.IntegrityError):
        with lab.connection(database) as conn:
            if mutation == "update":
                conn.execute(f"UPDATE {table_name} SET reason_detail='modified'")
            elif mutation == "delete":
                conn.execute(f"DELETE FROM {table_name}")
            else:
                columns = list(before[0])
                conn.execute(f"INSERT OR REPLACE INTO {table_name}({','.join(columns)}) "
                             f"VALUES({','.join('?' for _ in columns)})", tuple(before[0].values()))
    assert table(lab, database, table_name) == before


@pytest.mark.parametrize("changed,value", [("decision", "unknown"), ("expected_review_number", -1),
                                         ("expected_review_number", True), ("reason_code", ""),
                                         ("reason_detail", " "), ("issue_id", 0),
                                         ("expected_revision", False)])
def test_invalid_quality_request_is_rejected_before_claiming_operation(protected, changed, value):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store)
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    args = review_args(quality, first, issue_id)
    args[changed] = value
    before = database.read_bytes()
    with pytest.raises(StoreValidationError):
        quality.review_issue(first["id"], **args)
    assert database.read_bytes() == before


def test_reviews_and_assessments_do_not_recalculate_saved_predictions(protected):
    lab, database, store, clock = protected
    first, _ = scenario(store, clock)
    quality = QualityReviewStore(store, authority=SyntheticAuthority())
    before = immutable_intake(lab, database)
    events = store.get_events(first["id"])
    receipt = store.get(first["id"])
    issue_id = quality.context(first["id"])["issues"][0]["issue_id"]
    review(quality, first, issue_id, "hold")
    review(quality, first, issue_id, "exclude")
    review(quality, first, issue_id, "clear", actor=VERIFIED)
    assess(quality, first, "eligible", actor=VERIFIED)
    assert immutable_intake(lab, database) == before
    assert store.get_events(first["id"]) == events
    assert store.get(first["id"]) == receipt
    operations = table(lab, database, "staff_operations")
    request = json.loads(operations[-1]["request_json"])
    assert request["command"]["evidence"] == evidence()
    assert operations[-1]["principal_kind"] == VERIFIED["principal_kind"]
    assert operations[-1]["principal_id"] == VERIFIED["principal_id"]
    assert_reopened(database, clock, first)


def test_quality_writes_require_protected_mode_and_never_migrate_v1():
    for version, mode, enabled in [(1, "legacy", 0), (2, "legacy", 0)]:
        with synthetic_store(version=version, mode=mode, enabled=enabled) as (lab, database, store, clock):
            first = register(store)
            quality = QualityReviewStore(store)
            before = database.read_bytes()
            with pytest.raises(StateConflict):
                quality.assess(first["id"], operation_id=str(uuid4()), expected_revision=0,
                               write_epoch=str(uuid4()), expected_assessment_id=0,
                               expected_watermarks=dict(event_id=1, issue_id=0, review_id=0),
                               decision="hold", reason_code="pending", reason_detail="未確認",
                               evidence={}, **SHARED)
            assert database.read_bytes() == before


def test_quality_assessment_does_not_require_correction_feature_to_be_enabled():
    with synthetic_store(enabled=0) as (lab, database, store, clock):
        row = register(store)
        transition(store, row, "seat")
        quality = QualityReviewStore(store)
        assert assess(quality, row)["outcome"] == "assessment_added"
        assert store.get_data_quality(row["id"])["training_use"] == "blocked"
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT seat_corrections_enabled FROM store_metadata").fetchone()[0] == 0


def test_context_for_missing_reception_is_rejected_without_writes(protected):
    lab, database, store, clock = protected
    quality = QualityReviewStore(store)
    before = database.read_bytes()
    with pytest.raises(ReceptionNotFound):
        quality.context(str(uuid4()))
    assert database.read_bytes() == before
