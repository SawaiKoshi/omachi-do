"""Read-only schema/history checks shared by the store and explicit migration lab.

No application, predictor, filesystem, or migration entry point is imported here.
"""

from datetime import datetime
from hashlib import sha256
import json
import math
from uuid import UUID
from zoneinfo import ZoneInfo

from reception_migration.schema import expected_signature, signature
from seat_correction_quality import expected_issues
from staff_operation import RECEIPT_FIELDS, saved_response


class SchemaValidationError(ValueError):
    """An existing database cannot be used without manual investigation."""


STATE_FIELDS = ("status", "called_at", "seated_at", "cancelled_at", "actual_wait_minutes", "revision")
ALLOWED_TRANSITIONS = {
    "waiting": {"called", "seated", "cancelled"},
    "called": {"absent", "seated", "cancelled"},
    "absent": {"waiting", "called", "seated", "cancelled"},
    "seated": set(), "cancelled": set(),
}


def rows(conn, sql, parameters=()):
    return [tuple(row) for row in conn.execute(sql, parameters)]


def integrity(conn):
    if rows(conn, "PRAGMA integrity_check") != [("ok",)]:
        raise SchemaValidationError("SQLite integrity_check failed.")
    if rows(conn, "PRAGMA foreign_key_check"):
        raise SchemaValidationError("Foreign-key violations; data is not repaired automatically.")


def validate_schema(conn, version, *, check_integrity=True, allow_revision_required=False):
    if type(version) is not int or version not in (1, 2) or conn.execute("PRAGMA user_version").fetchone()[0] != version:
        raise SchemaValidationError(f"Expected schema v{version}; no automatic migration.")
    if signature(conn) != expected_signature(version):
        raise SchemaValidationError("Incomplete or unexpected schema, constraints, indexes, or triggers.")
    if check_integrity:
        integrity(conn)
    if version == 2:
        metadata = rows(conn, "SELECT singleton,database_id,write_epoch,staff_write_mode,"
                        "seat_corrections_enabled FROM store_metadata")
        if len(metadata) != 1 or metadata[0][0] != 1:
            raise SchemaValidationError("Invalid store metadata.")
        try:
            for value in metadata[0][1:3]:
                if str(UUID(value)) != value or UUID(value).int == 0:
                    raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise SchemaValidationError("Invalid database identity or write epoch.") from exc
        modes = ("legacy", "revision_required") if allow_revision_required else ("legacy",)
        if (metadata[0][3] not in modes or metadata[0][4] not in (0, 1)
                or (metadata[0][4] == 1 and metadata[0][3] != "revision_required")):
            raise SchemaValidationError("Unsupported staff mode or correction configuration.")
        if conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=2 AND from_version=1").fetchone()[0] != 1:
            raise SchemaValidationError("Missing v1 -> v2 migration provenance.")


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError("Missing or invalid timezone-aware historical timestamp.") from exc


def _records(conn, table):
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _object(value):
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError("Historical JSON must be an object.") from exc


def _same_state(actual, expected):
    if set(actual) != set(expected):
        return False
    for name, value in expected.items():
        candidate = actual[name]
        if name == "actual_wait_minutes" and value is not None:
            if (type(candidate) not in (int, float) or not math.isfinite(candidate)
                    or not math.isclose(candidate, value, rel_tol=1e-12, abs_tol=1e-9)):
                return False
        elif candidate != value or (name == "revision" and type(candidate) is not int):
            return False
    return True


def _same_json(actual, expected):
    """JSON booleans and numeric types are not interchangeable audit values."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(_same_json(actual[key], value)
                                                   for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_json(left, right)
                                                   for left, right in zip(actual, expected))
    return actual == expected


def validate_history(conn, version):
    """Replay committed operations, including append-only seating corrections.

    This verifies recorded-system consistency, not the truth of physical seating.
    Imported v1 events retain unknown revisions; their migration baseline is zero.
    """
    metadata = rows(conn, "SELECT singleton,last_recorded_at FROM store_metadata")
    if len(metadata) != 1 or metadata[0][0] != 1:
        raise SchemaValidationError("Missing singleton metadata.")
    last = timestamp(metadata[0][1]) if metadata[0][1] is not None else None
    receptions = {record["id"]: record for record in _records(conn, "receptions")}
    snapshots = {record["reception_id"]: record for record in _records(conn, "reception_snapshots")}
    predictions = {record["reception_id"]: record for record in _records(conn, "predictions")}
    keys = {record["reception_id"]: record for record in _records(conn, "idempotency_keys")}
    immutable_receipts = {}
    if any(set(records) != set(receptions) for records in (snapshots, predictions, keys)):
        raise SchemaValidationError("Missing or orphaned one-to-one intake data.")

    counters = {}
    for day, number in rows(conn, "SELECT reception_date,last_number FROM daily_counters"):
        try:
            if (not isinstance(day, str) or datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") != day
                    or type(number) is not int or number < 1):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise SchemaValidationError("Invalid daily reception counter.") from exc
        counters[day] = number
    for table, column in (("receptions", "reception_order"), ("reception_events", "event_id")):
        maximum = conn.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table}").fetchone()[0]
        sequence = rows(conn, "SELECT seq FROM sqlite_sequence WHERE name=?", (table,))
        if (len(sequence) > 1 or (maximum and not sequence)
                or (sequence and (type(sequence[0][0]) is not int or sequence[0][0] < max(0, maximum)))):
            raise SchemaValidationError("Invalid historical AUTOINCREMENT high-water mark.")

    for rid, reception in receptions.items():
        received = timestamp(reception["received_at"])
        try:
            day, suffix = reception["display_number"].rsplit("-", 1)
            valid_number = suffix.isdecimal() and int(suffix) > 0 and suffix == f"{int(suffix):03d}"
            if (not valid_number or day != received.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d")
                    or counters.get(day, 0) < int(suffix)):
                raise ValueError
        except (TypeError, ValueError, AttributeError) as exc:
            raise SchemaValidationError("Daily reception counter is inconsistent.") from exc
        if last is None or received > last or timestamp(reception["recorded_at"]) != received:
            raise SchemaValidationError("Reception clock is inconsistent.")
        if (keys[rid]["key"] is None or keys[rid]["adults"] != reception["adults"]
                or keys[rid]["children"] != reception["children"]):
            raise SchemaValidationError("Invalid intake idempotency data.")
        response = _object(keys[rid]["response_json"])
        if response.get("id") != rid or response.get("status") != "waiting":
            raise SchemaValidationError("Initial receipt no longer describes this intake.")
        snap = snapshots[rid]
        immutable = {name: reception[name] for name in RECEIPT_FIELDS
                     if name not in STATE_FIELDS and name not in ("snapshot", "prediction")}
        immutable["snapshot"] = {
            "captured_at": snap["captured_at"], "source": snap["source"],
            "waiting_groups": snap["waiting_groups"], "ahead_groups": snap["ahead_groups"],
            "ahead_same_size_groups": snap["ahead_same_size_groups"],
            "party_size_groups": {"1-2": snap["groups_1_2"], "3-4": snap["groups_3_4"],
                                  "5+": snap["groups_5_plus"]},
        }
        immutable["prediction"] = _object(predictions[rid]["result_json"])
        immutable_receipts[rid] = immutable
        # Migrated v1 receipts can be partial documents. Keep their exact JSON
        # intact; the canonical intake tables remain authoritative for fields
        # absent from that original cached response.
        if any(not _same_json(response[name], value) for name, value in immutable.items() if name in response):
            raise SchemaValidationError("Initial receipt changed immutable intake data.")
        if (timestamp(snapshots[rid]["captured_at"]) != received
                or timestamp(predictions[rid]["predicted_at"]) != received):
            raise SchemaValidationError("Intake snapshot/prediction timestamp changed.")

    states = {}
    previous_recorded = None
    previous_order = 0
    migration = None
    operations = {}
    audited_operations = set()
    revision_states = {}
    seen_events = {}
    latest_events = {}
    corrected_seatings = set()
    correction_events = []
    if version == 2:
        migration = rows(conn, "SELECT legacy_max_event_id,legacy_event_sequence,"
                         "legacy_reception_sequence FROM schema_migrations")[0]
        operations = {operation["operation_id"]: operation for operation in _records(conn, "staff_operations")}
    events = sorted(_records(conn, "reception_events"), key=lambda event: event["event_id"])
    for event in events:
        rid = event["reception_id"]
        if rid not in receptions:
            raise SchemaValidationError("Orphaned event.")
        reception = receptions[rid]
        received = timestamp(reception["received_at"])
        occurred, recorded = timestamp(event["occurred_at"]), timestamp(event["recorded_at"])
        if (occurred < received or recorded < occurred or last is None or recorded > last
                or (previous_recorded is not None and recorded < previous_recorded)):
            raise SchemaValidationError("Historical event clock is inconsistent.")
        previous_recorded = recorded
        kind, before, after = event["event_type"], event["from_status"], event["to_status"]
        audit = event.get("audit_kind", "legacy_v1")
        if version == 2:
            if ((audit == "legacy_v1" and event["event_id"] > migration[0])
                    or (audit != "legacy_v1" and event["event_id"] <= migration[1])):
                raise SchemaValidationError("Event crosses the preserved migration boundary.")
        if kind == "registered":
            if (rid in states or before is not None or after != "waiting" or occurred != received
                    or reception["reception_order"] <= previous_order):
                raise SchemaValidationError("Invalid registration event chain/order.")
            previous_order = reception["reception_order"]
            bands = [0, 0, 0]
            for other_id, state in states.items():
                if state["status"] in ("waiting", "called", "absent"):
                    people = receptions[other_id]["total_people"]
                    bands[0 if people <= 2 else 1 if people <= 4 else 2] += 1
            snap = snapshots[rid]
            band = 0 if reception["total_people"] <= 2 else 1 if reception["total_people"] <= 4 else 2
            expected = (sum(bands), sum(bands), bands[band], *bands)
            observed = tuple(snap[name] for name in ("waiting_groups", "ahead_groups",
                             "ahead_same_size_groups", "groups_1_2", "groups_3_4", "groups_5_plus"))
            if any(type(value) is not int for value in observed) or observed != expected:
                raise SchemaValidationError("Intake snapshot differs from the recorded event history.")
            state = dict(status="waiting", called_at=None, seated_at=None,
                         cancelled_at=None, actual_wait_minutes=None, revision=0)
            if version == 2 and audit != "legacy_v1":
                if audit != "registration_v2" or reception["reception_order"] <= migration[2]:
                    raise SchemaValidationError("Invalid v2 registration origin.")
                if not _same_state(_object(event["after_state_json"]), state):
                    raise SchemaValidationError("Invalid registration audit projection.")
        else:
            correction = kind == "seating_corrected"
            if correction:
                target = seen_events.get(event.get("corrects_event_id"))
                if (version != 2 or audit != "staff_v2" or rid not in states
                        or states[rid]["status"] != "seated" or before != "seated"
                        or after not in ("waiting", "called", "absent")
                        or target is None or target["reception_id"] != rid
                        or target["event_type"] != "seated" or target["from_status"] != after
                        or latest_events.get(rid) != target["event_id"]
                        or target["event_id"] in corrected_seatings):
                    raise SchemaValidationError("Invalid or stale historical correction target.")
                corrected_seatings.add(target["event_id"])
                correction_events.append(event)
            elif (rid not in states or states[rid]["status"] != before or kind != after
                    or after not in ALLOWED_TRANSITIONS.get(before, set())):
                raise SchemaValidationError("Unsupported or invalid historical state transition.")
            old_state = states[rid]
            state = dict(old_state)
            state.update(status=after, seated_at=None, cancelled_at=None, actual_wait_minutes=None)
            if after == "called" and not correction:
                state["called_at"] = event["occurred_at"]
            elif after == "seated":
                state["seated_at"] = event["occurred_at"]
                state["actual_wait_minutes"] = (occurred - received).total_seconds() / 60
            elif after == "cancelled":
                state["cancelled_at"] = event["occurred_at"]
            if version == 2 and audit != "legacy_v1":
                state["revision"] += 1
                if (audit not in ("staff_legacy_v2", "staff_v2")
                        or event["revision_before"] != old_state["revision"]
                        or event["revision_after"] != state["revision"]
                        or not _same_state(_object(event["before_state_json"]), old_state)
                        or not _same_state(_object(event["after_state_json"]), state)):
                    raise SchemaValidationError("Revision/audit state does not match the event chain.")
                if audit == "staff_v2":
                    operation = operations.get(event["operation_id"])
                    action = "seating_correction" if correction else {
                        "called": "call", "absent": "absent", "waiting": "return",
                        "seated": "seat", "cancelled": "cancel"}[after]
                    if (operation is None or operation["action"] != action
                            or operation["requested_reception_id"] != rid
                            or operation["resolved_reception_id"] != rid
                            or operation["outcome"] != "state_changed"
                            or operation["request_schema_version"] != 1
                            or operation["expected_revision"] != old_state["revision"]
                            or operation["resulting_revision"] != state["revision"]
                            or timestamp(operation["recorded_at"]) != recorded
                            or sha256(operation["request_json"].encode()).hexdigest() != operation["request_sha256"]):
                        raise SchemaValidationError("Staff operation does not match its audited state change.")
                    if correction:
                        command = _object(operation["request_json"])
                        if (command.get("target_event_id") != event["corrects_event_id"]
                                or command.get("reason_code") != event["reason_code"]
                                or command.get("reason_detail") != event["reason_detail"]):
                            raise SchemaValidationError("Correction request disagrees with its audit event.")
                    audited_operations.add(event["operation_id"])
        states[rid] = state
        revision_states[rid, state["revision"]] = (dict(state), recorded)
        seen_events[event["event_id"]] = event
        latest_events[rid] = event["event_id"]
    if set(states) != set(receptions):
        raise SchemaValidationError("Missing registration history.")
    if any(operation["outcome"] == "state_changed" and operation_id not in audited_operations
           for operation_id, operation in operations.items()):
        raise SchemaValidationError("Staff operation claims a state change without an audit event.")
    if version == 2:
        expected_quality = {}
        for correction in correction_events:
            for issue in expected_issues(events, seen_events[correction["corrects_event_id"]], correction):
                key = (issue["correction_event_id"], issue["reception_id"], issue["issue_kind"])
                if key in expected_quality:
                    raise SchemaValidationError("Duplicated expected correction quality issue.")
                expected_quality[key] = issue
        observed_quality = {}
        for issue in _records(conn, "data_quality_issues"):
            key = (issue["correction_event_id"], issue["reception_id"], issue["issue_kind"])
            if key in observed_quality:
                raise SchemaValidationError("Duplicated correction quality issue.")
            observed_quality[key] = {name: value for name, value in issue.items() if name != "issue_id"}
        if observed_quality != expected_quality:
            raise SchemaValidationError("Correction quality coverage/evidence does not match event history.")
        from quality_history import validate_quality_history
        validate_quality_history(conn)
    for operation in operations.values():
        try:
            response = saved_response(operation)
        except (ValueError, TypeError, KeyError) as exc:
            raise SchemaValidationError("Invalid archived staff request or response.") from exc
        recorded = timestamp(operation["recorded_at"])
        if last is None or recorded > last:
            raise SchemaValidationError("Staff operation clock exceeds the store clock.")
        rid, revision = operation["resolved_reception_id"], operation["resulting_revision"]
        if rid is not None:
            if rid not in receptions or revision is None or revision > receptions[rid]["revision"]:
                raise SchemaValidationError("Invalid staff operation reception/revision.")
            if recorded < timestamp(receptions[rid]["received_at"]):
                raise SchemaValidationError("Staff operation predates the reception.")
            historical, changed_at = revision_states.get((rid, revision), (None, None))
            next_state = revision_states.get((rid, revision + 1))
            if (historical is None or recorded < changed_at
                    or (next_state is not None and recorded > next_state[1])):
                raise SchemaValidationError("Staff operation clock does not match its result revision.")
        if operation["outcome"] in ("state_changed", "no_change"):
            historical, changed_at = revision_states.get((rid, revision), (None, None))
            receipt = response["reception"]
            if (historical is None or recorded < changed_at
                    or not _same_state({**{name: receipt[name] for name in STATE_FIELDS if name != "revision"},
                                        "revision": revision}, historical)):
                raise SchemaValidationError("Archived staff result disagrees with historical state.")
            if any(not _same_json(receipt[name], value) for name, value in immutable_receipts[rid].items()):
                raise SchemaValidationError("Archived staff result changed immutable intake data.")
    for rid, state in states.items():
        projection = {name: receptions[rid][name] for name in STATE_FIELDS if name != "revision"}
        projection["revision"] = receptions[rid].get("revision", 0)
        if not _same_state(projection, state):
            raise SchemaValidationError("Current state/wait/revision differs from recorded history.")
