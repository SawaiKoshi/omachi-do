"""Default-deny quality policy for one fixed, snapshot-free dataset contract.

There is deliberately no production identity/evidence verifier here. A caller
must supply a trusted verifier; the shared staff credential cannot release data.
"""

from datetime import datetime
import math

from quality_protocol import DATASET_CONTRACT, POLICY_VERSION, stored_request, verified_evidence


def records(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def watermarks(conn):
    return {name: conn.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table}").fetchone()[0]
            for name, table, column in (("event_id", "reception_events", "event_id"),
                                        ("issue_id", "data_quality_issues", "issue_id"),
                                        ("review_id", "data_quality_reviews", "review_id"))}


def context(conn, reception_id):
    receipts = records(conn, "SELECT revision,status FROM receptions WHERE id=?", (reception_id,))
    if not receipts:
        raise LookupError(reception_id)
    receipt = receipts[0]
    metadata = records(conn, "SELECT database_id,write_epoch FROM store_metadata")[0]
    events = records(conn, "SELECT * FROM reception_events WHERE reception_id=? ORDER BY event_id DESC LIMIT 1",
                     (reception_id,))
    seat = events[0] if events and events[0]["event_type"] == "seated" and receipt["status"] == "seated" else None
    issues = records(conn, "SELECT * FROM data_quality_issues WHERE reception_id=? ORDER BY issue_id", (reception_id,))
    for issue in issues:
        reviews = records(conn, "SELECT * FROM data_quality_reviews WHERE issue_id=? ORDER BY review_id DESC LIMIT 1",
                          (issue["issue_id"],))
        issue["latest_review"] = reviews[0] if reviews else None
    latest = conn.execute("SELECT COALESCE(MAX(assessment_id),0) FROM data_quality_assessments WHERE reception_id=?",
                          (reception_id,)).fetchone()[0]
    return dict(reception_id=reception_id, revision=receipt["revision"], write_epoch=metadata["write_epoch"],
                watermarks=watermarks(conn), latest_assessment_id=latest,
                valid_seated_event_id=seat["event_id"] if seat else None, issues=issues)


def authorization_context(command, seat):
    return dict(reception_id=command["reception_id"], revision=command["expected_revision"],
                valid_seated_event_id=seat, watermarks=command["expected_watermarks"],
                issue_id=command.get("issue_id"), dataset_contract=DATASET_CONTRACT)


def authorize(authority, principal_kind, principal_id, permission, evidence, command, seat):
    if authority is None or principal_kind != "staff_account" or seat is None or not verified_evidence(evidence):
        return None
    try:
        identity = authority.authority_id
        if (not isinstance(identity, str) or not identity.strip() or len(identity) > 64
                or authority.dataset_contract != DATASET_CONTRACT
                or authority.verify(principal_kind, principal_id, permission, evidence,
                                    authorization_context(command, seat)) is not True):
            return None
    except Exception:
        # Provider failure never upgrades an unverified decision to permission.
        return None
    return dict(authority_id=identity, dataset_contract=DATASET_CONTRACT,
                valid_seated_event_id=seat, reception_revision=command["expected_revision"])


def authorization_current(authority, operation, wrapper):
    grant = wrapper["authorization"]
    if grant is None:
        return False
    checked = authorize(authority, operation["principal_kind"], operation["principal_id"],
                        wrapper["command"]["decision"], wrapper["command"]["evidence"],
                        wrapper["command"], grant["valid_seated_event_id"])
    return checked == grant


def review_clear_reasons(conn, reception_id, revision, valid_seated_event_id, marks,
                        *, authority=None, verify_authority=False):
    """Check latest reviews at a historical watermark, or with a live verifier.

    Current extraction must separately require a fresh assessment. A later review
    does not retroactively invalidate the structural validity of past assessments.
    """
    reasons = set()
    issues = records(conn, "SELECT * FROM data_quality_issues WHERE reception_id=? AND issue_id<=? ORDER BY issue_id",
                     (reception_id, marks["issue_id"]))
    epoch = conn.execute("SELECT write_epoch FROM store_metadata").fetchone()[0]
    for issue in issues:
        reviews = records(conn, "SELECT * FROM data_quality_reviews WHERE issue_id=? AND review_id<=? "
                          "ORDER BY review_id DESC LIMIT 1", (issue["issue_id"], marks["review_id"]))
        if not reviews:
            reasons.add("unreviewed_issue")
            continue
        review = reviews[0]
        if review["decision"] != "clear":
            reasons.add("excluded_issue" if review["decision"] == "exclude" else "held_issue")
            continue
        operations = records(conn, "SELECT * FROM staff_operations WHERE operation_id=?", (review["operation_id"],))
        if not operations:
            reasons.add("invalid_review_operation")
            continue
        op = operations[0]
        try:
            wrapper = stored_request(op)
            command, grant = wrapper["command"], wrapper["authorization"]
            if (op["expected_revision"] != revision or grant is None
                    or grant["valid_seated_event_id"] != valid_seated_event_id
                    or command["expected_watermarks"]["event_id"] != marks["event_id"]
                    or command["expected_watermarks"]["issue_id"] != marks["issue_id"]):
                reasons.add("stale_review")
            if verify_authority and (op["write_epoch"] != epoch or not authorization_current(authority, op, wrapper)):
                reasons.add("unverified_review_authority")
        except (ValueError, TypeError, KeyError):
            reasons.add("invalid_review_operation")
    return sorted(reasons)


def candidate_reasons(conn, reception_id, *, authority=None):
    reasons = set()
    current = context(conn, reception_id)
    seat_id, revision, marks = current["valid_seated_event_id"], current["revision"], current["watermarks"]
    if authority is None:
        reasons.add("authority_unavailable")
    if seat_id is None:
        reasons.add("no_valid_seating")
    assessments = records(conn, "SELECT * FROM data_quality_assessments WHERE reception_id=? "
                           "ORDER BY assessment_id DESC LIMIT 1", (reception_id,))
    if not assessments:
        reasons.add("not_assessed")
    else:
        assessment = assessments[0]
        if assessment["decision"] != "eligible":
            reasons.add("assessment_not_eligible")
        if assessment["policy_version"] != POLICY_VERSION:
            reasons.add("unknown_policy")
        if assessment["reception_revision"] != revision or assessment["valid_seated_event_id"] != seat_id:
            reasons.add("stale_assessment_revision")
        if {"event_id": assessment["checked_through_event_id"], "issue_id": assessment["issues_through_id"],
            "review_id": assessment["reviews_through_id"]} != marks:
            reasons.add("stale_assessment_context")
        operations = records(conn, "SELECT * FROM staff_operations WHERE operation_id=?", (assessment["operation_id"],))
        try:
            op = operations[0]
            if op["write_epoch"] != current["write_epoch"] or not authorization_current(authority, op, stored_request(op)):
                reasons.add("unverified_assessment_authority")
        except (IndexError, ValueError, TypeError, KeyError):
            reasons.add("invalid_assessment_operation")
    reasons.update(review_clear_reasons(conn, reception_id, revision, seat_id, marks,
                                       authority=authority, verify_authority=True))
    if seat_id is not None:
        receipt = records(conn, "SELECT * FROM receptions WHERE id=?", (reception_id,))[0]
        seat = records(conn, "SELECT * FROM reception_events WHERE event_id=?", (seat_id,))[0]
        try:
            received, seated = datetime.fromisoformat(receipt["received_at"]), datetime.fromisoformat(receipt["seated_at"])
            wait = receipt["actual_wait_minutes"]
            if (received.utcoffset() is None or seated.utcoffset() is None or seated < received
                    or receipt["seated_at"] != seat["occurred_at"] or type(wait) not in (int, float)
                    or not math.isfinite(wait) or wait < 0
                    or not math.isclose(wait, (seated-received).total_seconds()/60, rel_tol=1e-12, abs_tol=1e-9)):
                reasons.add("invalid_actual_wait")
        except (ValueError, TypeError):
            reasons.add("invalid_actual_wait")
    return sorted(reasons)
