"""Versioned staff command format; no credentials, database I/O, or prediction code."""

import json
from hashlib import sha256
from uuid import UUID


ACTION_TARGETS = {
    "call": "called", "absent": "absent", "return": "waiting",
    "seat": "seated", "cancel": "cancelled",
}
REQUEST_VERSION = 1
MAX_REVISION = 2**63 - 1
RECEIPT_FIELDS = (
    "id", "display_number", "reception_order", "received_at", "adults", "children",
    "total_people", "status", "called_at", "seated_at", "cancelled_at",
    "actual_wait_minutes", "snapshot", "prediction",
)


def receipt_response(record):
    return {name: record[name] for name in RECEIPT_FIELDS}


def uuid_text(value):
    parsed = UUID(str(value))
    if parsed.int == 0:
        raise ValueError("A non-nil UUID is required.")
    return str(parsed)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def command_request(reception_id, action, expected_revision, write_epoch):
    if not isinstance(action, str) or action not in ACTION_TARGETS:
        raise ValueError("Unsupported staff action.")
    if type(expected_revision) is not int or not 0 <= expected_revision <= MAX_REVISION:
        raise ValueError("A nonnegative SQLite integer revision is required.")
    return dict(request_schema_version=REQUEST_VERSION,
                reception_id=uuid_text(reception_id), action=action,
                expected_revision=expected_revision, write_epoch=uuid_text(write_epoch))


def correction_request(reception_id, expected_revision, write_epoch, target_event_id,
                       reason_code, reason_detail):
    command = command_request(reception_id, "seat", expected_revision, write_epoch)
    if type(target_event_id) is not int or not 1 <= target_event_id <= MAX_REVISION:
        raise ValueError("A positive SQLite event ID is required.")
    if (reason_code not in ("wrong_reception", "accidental_seating", "other")
            or not isinstance(reason_detail, str) or not reason_detail.strip()
            or len(reason_detail) > 500):
        raise ValueError("A correction reason and nonblank detail are required.")
    command.update(action="seating_correction", target_event_id=target_event_id,
                   reason_code=reason_code, reason_detail=reason_detail)
    return command


def validate_principal(kind, principal_id):
    if (kind not in ("shared_staff_token", "staff_account", "system")
            or not isinstance(principal_id, str) or not 1 <= len(principal_id.strip())
            or len(principal_id) > 128):
        raise ValueError("Invalid authenticated principal.")


def saved_response(operation):
    """Fail closed on a damaged request/response ledger, including rejected results."""
    if operation["action"] == "seating_correction":
        request = json.loads(operation["request_json"])
        command = correction_request(operation["requested_reception_id"], operation["expected_revision"],
                                     operation["write_epoch"], request["target_event_id"],
                                     request["reason_code"], request["reason_detail"])
    else:
        command = command_request(operation["requested_reception_id"], operation["action"],
                                  operation["expected_revision"], operation["write_epoch"])
    validate_principal(operation["principal_kind"], operation["principal_id"])
    encoded = canonical_json(command)
    if (operation["request_schema_version"] != REQUEST_VERSION
            or operation["request_json"] != encoded
            or operation["request_sha256"] != sha256(encoded.encode()).hexdigest()):
        raise ValueError("Stored staff request is inconsistent.")
    response = json.loads(operation["response_json"])
    if (not isinstance(response, dict) or set(response) != {
            "operation_id", "reception_id", "action", "outcome", "response_status",
            "recorded_at", "resulting_revision", "reception", "error"}
            or response["operation_id"] != uuid_text(operation["operation_id"])
            or response["reception_id"] != command["reception_id"]
            or any(response[name] != operation[name] for name in (
                "action", "outcome", "response_status", "recorded_at", "resulting_revision"))):
        raise ValueError("Stored staff response is inconsistent.")
    revision = response["resulting_revision"]
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ValueError("Invalid stored result revision.")
    if response["outcome"] == "rejected":
        error = response["error"]
        if (response["reception"] is not None or not isinstance(response["error"], dict)
                or set(error) != {"code", "detail"}
                or not isinstance(error["code"], str) or not isinstance(error["detail"], str)
                or not error["detail"].strip()
                or error["code"] not in {"write_epoch_conflict", "revision_conflict", "state_conflict", "reception_not_found", "correction_target_conflict"}
                or response["response_status"] != (404 if error["code"] == "reception_not_found" else 409)
                or (error["code"] == "reception_not_found" and operation["resolved_reception_id"] is not None)
                or (error["code"] in ("revision_conflict", "state_conflict")
                    and operation["resolved_reception_id"] is None)):
            raise ValueError("Invalid stored rejection.")
    elif response["outcome"] in ("state_changed", "no_change"):
        receipt = response["reception"]
        correction = command["action"] == "seating_correction"
        if (response["response_status"] != 200 or response["error"] is not None
                or not isinstance(receipt, dict) or set(receipt) != set(RECEIPT_FIELDS)
                or receipt.get("id") != command["reception_id"]
                or (receipt.get("status") not in ("waiting", "called", "absent") if correction
                    else receipt.get("status") != ACTION_TARGETS[command["action"]])
                or (correction and response["outcome"] != "state_changed")
                or operation["resolved_reception_id"] != command["reception_id"]
                or revision != command["expected_revision"] + (response["outcome"] == "state_changed")):
            raise ValueError("Invalid stored successful result.")
    else:
        raise ValueError("Unsupported stored outcome.")
    return response
