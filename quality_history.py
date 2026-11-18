"""Replay append-only SQLite quality decisions without granting training access.

These checks authenticate the consistency of the recorded protocol. They do not
authenticate a human's historical observation or enable a model-training path.
"""

import json


def _records(conn, table):
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _same(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(right, dict):
        return set(left) == set(right) and all(_same(left[key], value) for key, value in right.items())
    if isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def validate_quality_history(conn):
    """Reject orphaned, stale or inconsistent review and assessment histories.

    The surrounding reception-history validator verifies all state projections,
    correction coverage and the archived command protocol. This function also
    checks successful quality commands in both directions, so a dropped child
    record cannot silently remove an exclusion from the training boundary.
    """
    # Runtime import avoids a circular dependency with the shared history entry
    # point while preserving its one public failure type.
    from reception_schema import SchemaValidationError, timestamp
    from quality_protocol import saved_quality_response, stored_request

    def fail(detail):
        raise SchemaValidationError(detail)

    issues = {row["issue_id"]: row for row in _records(conn, "data_quality_issues")}
    events = {row["event_id"]: row for row in _records(conn, "reception_events")}
    reviews = sorted(_records(conn, "data_quality_reviews"), key=lambda row: row["review_id"])
    assessments = sorted(_records(conn, "data_quality_assessments"), key=lambda row: row["assessment_id"])
    operations = {row["operation_id"]: row for row in _records(conn, "staff_operations")}
    receptions = {row["id"]: row for row in _records(conn, "receptions")}
    review_by_id = {row["review_id"]: row for row in reviews}
    if len(review_by_id) != len(reviews):
        fail("Duplicate quality review identity.")
    quality_operations = [operation for operation in operations.values()
                          if operation["action"] in ("quality_review", "quality_assessment")]
    mode = conn.execute("SELECT staff_write_mode FROM store_metadata WHERE singleton=1").fetchone()[0]
    if (reviews or assessments or quality_operations) and mode != "revision_required":
        # Quality commands are created only in this mode; its downgrade trigger
        # makes any such legacy history evidence of out-of-protocol mutation.
        fail("Quality decisions cannot originate from legacy staff mode.")
    parent_ids = set()
    requests = {}
    for operation_id, operation in operations.items():
        if operation["action"] in ("quality_review", "quality_assessment"):
            try:
                requests[operation_id] = stored_request(operation)
                saved_quality_response(operation)
            except (KeyError, TypeError, ValueError) as exc:
                raise SchemaValidationError("Malformed archived quality protocol.") from exc

    for table, column, records in (("data_quality_issues", "issue_id", list(issues.values())),
                                    ("data_quality_reviews", "review_id", reviews),
                                    ("data_quality_assessments", "assessment_id", assessments)):
        maximum = max((row[column] for row in records), default=0)
        found = list(conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)))
        if (len(found) > 1 or (maximum and not found)
                or (found and (type(found[0][0]) is not int or found[0][0] < maximum))):
            fail("Invalid quality AUTOINCREMENT high-water mark.")

    def operation_for(child, action, outcome):
        op_id = child["operation_id"]
        operation = operations.get(op_id)
        if op_id in parent_ids or operation is None:
            fail("Missing or multiply used quality operation.")
        parent_ids.add(op_id)
        if (operation["action"] != action or operation["outcome"] != outcome
                or operation["response_status"] != 200
                or operation["requested_reception_id"] != child["reception_id"]
                or operation["resolved_reception_id"] != child["reception_id"]
                or operation["expected_revision"] != operation["resulting_revision"]
                or operation["recorded_at"] != child["recorded_at"]):
            fail("Quality decision disagrees with its operation.")
        try:
            request = requests[operation["operation_id"]]
            command = request["command"]
            response = json.loads(operation["response_json"])
            if not _same(response["quality_result"], child):
                fail("Archived quality response disagrees with its child record.")
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaValidationError("Malformed archived quality decision.") from exc
        for name in ("decision", "reason_code", "reason_detail"):
            if not _same(command.get(name), child[name]):
                fail("Quality reason or decision differs from its command.")
        return operation, command

    def check_watermarks(command, recorded_at):
        try:
            marks = command["expected_watermarks"]
            if set(marks) != {"event_id", "issue_id", "review_id"}:
                raise ValueError
            for name, records in (("event_id", events), ("issue_id", issues), ("review_id", review_by_id)):
                value = marks[name]
                if type(value) is not int or value < 0 or (value and value not in records):
                    raise ValueError
                # Equal-time events are ordered by their IDs, not by timestamps.
                if any(timestamp(row["recorded_at"]) > timestamp(recorded_at)
                       for key, row in records.items() if key <= value):
                    raise ValueError
                if any(timestamp(row["recorded_at"]) < timestamp(recorded_at)
                       for key, row in records.items() if key > value):
                    raise ValueError
            # Correction and all of its issues commit together. A historical
            # event boundary therefore determines the complete issue boundary.
            expected_issue = max((key for key, issue in issues.items()
                                  if issue["correction_event_id"] <= marks["event_id"]), default=0)
            if marks["issue_id"] != expected_issue:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaValidationError("Invalid or incomplete quality watermarks.") from exc
        return marks

    def historical_context(rid, marks):
        historical = sorted((event for event in events.values()
                             if event["reception_id"] == rid and event["event_id"] <= marks["event_id"]),
                            key=lambda event: event["event_id"])
        latest = historical[-1] if historical else None
        valid_seated = latest["event_id"] if latest is not None and latest["event_type"] == "seated" else None
        revision = (latest["revision_after"] or 0) if latest is not None else None
        return valid_seated, revision

    previous_review_id = 0
    review_numbers = {}
    previous_review_time = None
    for review in reviews:
        operation, command = operation_for(review, "quality_review", "review_added")
        issue = issues.get(review["issue_id"])
        if issue is None or issue["reception_id"] != review["reception_id"]:
            fail("Quality review references a different or missing issue.")
        previous_number = review_numbers.get(review["issue_id"], 0)
        if (review["review_number"] != previous_number + 1
                or command.get("issue_id") != review["issue_id"]
                or command.get("expected_review_number") != previous_number):
            fail("Noncontiguous or stale quality review number.")
        marks = check_watermarks(command, review["recorded_at"])
        recorded = timestamp(review["recorded_at"])
        valid_seated, historical_revision = historical_context(review["reception_id"], marks)
        if (marks["review_id"] != previous_review_id or marks["issue_id"] < review["issue_id"]
                or operation["expected_revision"] != historical_revision
                or (previous_review_time is not None and recorded < previous_review_time)):
            fail("Quality review did not observe the preceding quality history.")
        if review["decision"] == "clear":
            grant = requests[operation["operation_id"]]["authorization"]
            if (valid_seated is None or grant is None
                    or grant["valid_seated_event_id"] != valid_seated
                    or grant["reception_revision"] != historical_revision):
                fail("Cleared review does not verify the historical seating and revision.")
        review_numbers[review["issue_id"]] = review["review_number"]
        previous_review_id, previous_review_time = review["review_id"], recorded

    previous_assessment = {}
    previous_assessment_time = None
    for assessment in assessments:
        operation, command = operation_for(assessment, "quality_assessment", "assessment_added")
        rid = assessment["reception_id"]
        if rid not in receptions:
            fail("Assessment references a missing reception.")
        marks = check_watermarks(command, assessment["recorded_at"])
        recorded = timestamp(assessment["recorded_at"])
        if (command.get("expected_assessment_id") != previous_assessment.get(rid, 0)
                or assessment["reception_revision"] != operation["expected_revision"]
                or assessment["policy_version"] != command.get("policy_version")
                or assessment["checked_through_event_id"] != marks["event_id"]
                or assessment["issues_through_id"] != marks["issue_id"]
                or assessment["reviews_through_id"] != marks["review_id"]
                or (previous_assessment_time is not None and recorded < previous_assessment_time)):
            fail("Stale or inconsistent quality assessment.")
        valid_seated, historical_revision = historical_context(rid, marks)
        if (assessment["valid_seated_event_id"] != valid_seated
                or assessment["reception_revision"] != historical_revision):
            fail("Quality assessment references a stale seating or revision.")
        if assessment["decision"] == "eligible":
            if valid_seated is None:
                fail("Eligible quality assessment has no valid seated event.")
            for issue in issues.values():
                if issue["reception_id"] != rid or issue["issue_id"] > marks["issue_id"]:
                    continue
                candidates = [review for review in reviews if review["issue_id"] == issue["issue_id"]
                              and review["review_id"] <= marks["review_id"]]
                latest_review = max(candidates, key=lambda review: review["review_id"], default=None)
                if latest_review is None or latest_review["decision"] != "clear":
                    fail("Eligible assessment contains an unresolved historical issue.")
            grant = requests[operation["operation_id"]]["authorization"]
            if (grant is None or grant["valid_seated_event_id"] != valid_seated
                    or grant["reception_revision"] != historical_revision):
                fail("Eligible assessment authorization references a different seating.")
            # Current extraction freshness is deliberately not checked here:
            # later append-only events must not invalidate a sound old audit.
            from quality_policy import review_clear_reasons
            if review_clear_reasons(conn, rid, historical_revision, valid_seated, marks):
                fail("Eligible assessment has stale or unverified historical issue reviews.")
        previous_assessment[rid] = assessment["assessment_id"]
        previous_assessment_time = recorded

    for operation in operations.values():
        if operation["action"] in ("quality_review", "quality_assessment"):
            expected = "review_added" if operation["action"] == "quality_review" else "assessment_added"
            if operation["outcome"] == expected and operation["operation_id"] not in parent_ids:
                fail("Successful quality operation is missing its immutable decision.")
            if operation["outcome"] not in (expected, "rejected"):
                fail("Unsupported quality operation outcome.")
