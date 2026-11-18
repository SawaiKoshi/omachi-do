"""Canonical quality commands and archived outcomes, separate from staff transitions."""

from hashlib import sha256
import json

from staff_operation import MAX_REVISION, canonical_json, command_request, validate_principal


POLICY_VERSION = "sqlite-quality-v1"
DATASET_CONTRACT = "rf-intake-actual-wait-v1"
REVIEW_COLUMNS = {"review_id", "issue_id", "reception_id", "review_number", "operation_id",
                  "decision", "reason_code", "reason_detail", "recorded_at"}
ASSESSMENT_COLUMNS = {"assessment_id", "reception_id", "reception_revision", "valid_seated_event_id",
                      "operation_id", "decision", "policy_version", "checked_through_event_id",
                      "issues_through_id", "reviews_through_id", "reason_code", "reason_detail", "recorded_at"}


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_REVISION:
        raise ValueError("Invalid SQLite integer.")
    return value


def text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("A nonblank reason/reference is required.")
    return value


def marks(value):
    if not isinstance(value, dict) or set(value) != {"event_id", "issue_id", "review_id"}:
        raise ValueError("A complete quality watermark is required.")
    return {key: integer(value[key]) for key in ("event_id", "issue_id", "review_id")}


def verified_evidence(value):
    return (isinstance(value, dict) and set(value) == {
        "reference", "intake_verified", "seating_verified", "context_understood"}
        and isinstance(value["reference"], str) and bool(value["reference"].strip())
        and len(value["reference"]) <= 128
        and all(value[name] is True for name in ("intake_verified", "seating_verified", "context_understood")))


def quality_request(action, reception_id, expected_revision, write_epoch, *, expected_watermarks,
                    decision, reason_code, reason_detail, evidence, issue_id=None,
                    expected_review_number=None, expected_assessment_id=None):
    command = command_request(reception_id, "seat", expected_revision, write_epoch)
    decisions = {"quality_review": {"hold", "exclude", "clear"},
                 "quality_assessment": {"hold", "exclude", "eligible"}}
    if action not in decisions or not isinstance(decision, str) or decision not in decisions[action]:
        raise ValueError("Unknown quality decision.")
    if not isinstance(evidence, dict) or (evidence != {} and not verified_evidence(evidence)):
        raise ValueError("Invalid evidence structure.")
    if decision in ("clear", "eligible") and not verified_evidence(evidence):
        raise ValueError("Verified evidence is required to release a hold.")
    command.update(action=action, expected_watermarks=marks(expected_watermarks), decision=decision,
                   reason_code=text(reason_code, 64), reason_detail=text(reason_detail, 500),
                   evidence=json.loads(canonical_json(evidence)))
    if action == "quality_review":
        command.update(issue_id=integer(issue_id, 1), expected_review_number=integer(expected_review_number))
    else:
        command.update(expected_assessment_id=integer(expected_assessment_id), policy_version=POLICY_VERSION)
    return command


def stored_request(operation):
    """Validate the client command and server-generated authorization independently."""
    operation = dict(operation)
    wrapper = json.loads(operation["request_json"])
    if not isinstance(wrapper, dict) or set(wrapper) != {"command", "authorization"}:
        raise ValueError("Unknown quality request wrapper.")
    command = wrapper["command"]
    if not isinstance(command, dict):
        raise ValueError("Invalid quality command.")
    params = {key: command[key] for key in ("expected_watermarks", "decision", "reason_code", "reason_detail", "evidence")}
    if operation["action"] == "quality_review":
        params.update(issue_id=command["issue_id"], expected_review_number=command["expected_review_number"])
    else:
        params.update(expected_assessment_id=command["expected_assessment_id"])
    expected = quality_request(operation["action"], operation["requested_reception_id"],
                               operation["expected_revision"], operation["write_epoch"], **params)
    if (canonical_json(command) != canonical_json(expected) or operation["request_schema_version"] != 1
            or canonical_json(wrapper) != operation["request_json"]
            or sha256(operation["request_json"].encode()).hexdigest() != operation["request_sha256"]):
        raise ValueError("Inconsistent quality request.")
    validate_principal(operation["principal_kind"], operation["principal_id"])
    grant = wrapper["authorization"]
    if grant is not None:
        if (not isinstance(grant, dict) or set(grant) != {
                "authority_id", "dataset_contract", "valid_seated_event_id", "reception_revision"}
                or grant["dataset_contract"] != DATASET_CONTRACT
                or grant["reception_revision"] != command["expected_revision"]
                or operation["principal_kind"] != "staff_account"):
            raise ValueError("Invalid recorded quality authorization.")
        text(grant["authority_id"], 64)
        integer(grant["reception_revision"])
        integer(grant["valid_seated_event_id"], 1)
    if (operation["outcome"] != "rejected" and command["decision"] in ("clear", "eligible")
            and grant is None):
        raise ValueError("A release has no verified authority.")
    if command["decision"] in ("hold", "exclude") and grant is not None:
        raise ValueError("Unexpected release authorization.")
    return wrapper


def saved_quality_response(operation):
    operation = dict(operation)
    wrapper = stored_request(operation)
    command = wrapper["command"]
    response = json.loads(operation["response_json"])
    if (not isinstance(response, dict) or set(response) != {
            "operation_id", "reception_id", "action", "outcome", "response_status", "recorded_at",
            "resulting_revision", "quality_result", "error"}
            or response["reception_id"] != command["reception_id"]
            or any(response[name] != operation[name] for name in (
                "operation_id", "action", "outcome", "response_status", "recorded_at", "resulting_revision"))):
        raise ValueError("Inconsistent quality result.")
    if response["resulting_revision"] is not None:
        integer(response["resulting_revision"])
    if operation["outcome"] == "rejected":
        error = response["error"]
        codes = {"quality_authorization_required": 403, "reception_not_found": 404,
                 "quality_issue_not_found": 404, "write_epoch_conflict": 409,
                 "revision_conflict": 409, "quality_context_conflict": 409,
                 "quality_sequence_conflict": 409, "quality_not_eligible": 409}
        if (response["quality_result"] is not None or not isinstance(error, dict)
                or set(error) != {"code", "detail"} or not isinstance(error["code"], str)
                or codes.get(error["code"]) != response["response_status"]):
            raise ValueError("Invalid quality rejection.")
        text(error["detail"], 500)
    else:
        review = operation["action"] == "quality_review"
        child = response["quality_result"]
        if (operation["outcome"] != ("review_added" if review else "assessment_added")
                or response["response_status"] != 200 or response["error"] is not None
                or response["resulting_revision"] != command["expected_revision"]
                or operation["resolved_reception_id"] != command["reception_id"]
                or not isinstance(child, dict) or set(child) != (REVIEW_COLUMNS if review else ASSESSMENT_COLUMNS)
                or any(child[name] != command[name] for name in ("reception_id", "decision", "reason_code", "reason_detail"))
                or child["operation_id"] != operation["operation_id"]
                or child["recorded_at"] != operation["recorded_at"]):
            raise ValueError("Invalid successful quality result.")
    return response
