"""Conservative, versioned impact evidence derived from immutable event order.

This records potential harm, never rewrites past snapshots/predictions and never
grants permission to train. Event IDs, rather than clock precision, bound impact.
"""

from staff_operation import canonical_json


POLICY_VERSION = "seat-correction-v1"
ACTIVE = {"waiting", "called", "absent"}


def expected_issues(events, target_event, correction_event):
    target_id, correction_id = target_event["event_id"], correction_event["event_id"]
    source_id = target_event["reception_id"]
    if (target_id >= correction_id or target_event["event_type"] != "seated"
            or correction_event["event_type"] != "seating_corrected"
            or correction_event["corrects_event_id"] != target_id
            or correction_event["reception_id"] != source_id):
        raise ValueError("Invalid quality impact interval.")
    issues = []

    def add(reception_id, kind, evidence_ids):
        issues.append(dict(
            reception_id=reception_id, correction_event_id=correction_id,
            issue_kind=kind, evidence_event_id=evidence_ids[0], policy_version=POLICY_VERSION,
            evidence_json=canonical_json(dict(target_event_id=target_id,
                                             correction_event_id=correction_id,
                                             evidence_event_ids=evidence_ids)),
            recorded_at=correction_event["recorded_at"],
        ))

    add(source_id, "invalidated_seating", [target_id])
    active, workflow = set(), {}
    for event in sorted(events, key=lambda row: row["event_id"]):
        event_id, rid = event["event_id"], event["reception_id"]
        if event_id >= correction_id:
            break
        if target_id < event_id and rid != source_id:
            if event["event_type"] == "registered":
                add(rid, "snapshot_context", [event_id])
                # Waiting groups are NOT model features. A positive count only
                # enables inference; numeric count changes do not alter it.
                if not active:
                    add(rid, "prediction_gate", [event_id])
            else:
                workflow.setdefault(rid, []).append(event_id)
        if event["to_status"] in ACTIVE:
            active.add(rid)
        else:
            active.discard(rid)
    for rid, evidence_ids in sorted(workflow.items()):
        add(rid, "workflow_impact", evidence_ids)
    return issues
