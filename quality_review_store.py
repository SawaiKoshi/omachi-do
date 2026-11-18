"""Append-only reviews and assessments; release authority is absent by default."""

from hashlib import sha256

from quality_policy import authorize, context, review_clear_reasons
from quality_protocol import POLICY_VERSION, quality_request, stored_request
from reception_schema import validate_history
from reception_store import ReceptionNotFound, StateConflict, StoreValidationError
from staff_operation import canonical_json, uuid_text, validate_principal


class QualityReviewStore:
    def __init__(self, store, *, authority=None):
        self.store = store
        self.authority = authority

    def context(self, reception_id):
        self.store._require_staff_v2()
        with self.store._connection() as conn:
            validate_history(conn, 2)
            try:
                return context(conn, reception_id)
            except LookupError as exc:
                raise ReceptionNotFound(reception_id) from exc

    @staticmethod
    def _save_review(conn, row):
        cursor = conn.execute("INSERT INTO data_quality_reviews(" + ",".join(row) + ") VALUES(" +
                              ",".join("?" for _ in row) + ")", tuple(row.values()))
        return dict(conn.execute("SELECT * FROM data_quality_reviews WHERE review_id=?", (cursor.lastrowid,)).fetchone())

    @staticmethod
    def _save_assessment(conn, row):
        cursor = conn.execute("INSERT INTO data_quality_assessments(" + ",".join(row) + ") VALUES(" +
                              ",".join("?" for _ in row) + ")", tuple(row.values()))
        return dict(conn.execute("SELECT * FROM data_quality_assessments WHERE assessment_id=?", (cursor.lastrowid,)).fetchone())

    def review_issue(self, reception_id, *, operation_id, expected_revision, write_epoch, issue_id,
                     expected_review_number, expected_watermarks, decision, reason_code, reason_detail,
                     evidence, principal_kind, principal_id):
        return self._command("quality_review", reception_id, operation_id, expected_revision, write_epoch,
                             principal_kind, principal_id, issue_id=issue_id,
                             expected_review_number=expected_review_number, expected_watermarks=expected_watermarks,
                             decision=decision, reason_code=reason_code, reason_detail=reason_detail, evidence=evidence)

    def assess(self, reception_id, *, operation_id, expected_revision, write_epoch, expected_assessment_id,
               expected_watermarks, decision, reason_code, reason_detail, evidence, principal_kind, principal_id):
        return self._command("quality_assessment", reception_id, operation_id, expected_revision, write_epoch,
                             principal_kind, principal_id, expected_assessment_id=expected_assessment_id,
                             expected_watermarks=expected_watermarks, decision=decision,
                             reason_code=reason_code, reason_detail=reason_detail, evidence=evidence)

    def _command(self, action, reception_id, operation_id, expected_revision, write_epoch,
                 principal_kind, principal_id, **parameters):
        self.store._require_staff_v2()
        try:
            operation_id = uuid_text(operation_id)
            validate_principal(principal_kind, principal_id)
            command = quality_request(action, reception_id, expected_revision, write_epoch, **parameters)
        except (ValueError, TypeError) as exc:
            raise StoreValidationError("品質確認の要求が不正です。") from exc
        rid = command["reception_id"]
        with self.store._connection(write=True) as conn:
            # A persisted result cannot hide a missing review/assessment child
            # or incomplete correction coverage, even on a read-only replay.
            validate_history(conn, 2)
            previous = conn.execute("SELECT * FROM staff_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if previous is not None:
                if (previous["action"] != action or previous["principal_kind"] != principal_kind
                        or previous["principal_id"] != principal_id):
                    raise StateConflict("操作IDが別の要求または認証主体で使用されています。")
                try:
                    archived = stored_request(previous)
                except (ValueError, TypeError, KeyError) as exc:
                    raise StoreValidationError("品質操作の記録が不整合です。") from exc
                if canonical_json(archived["command"]) != canonical_json(command):
                    raise StateConflict("操作IDが別の要求で使用されています。")
                return self.store._saved_staff_response(previous)
            metadata = conn.execute("SELECT write_epoch,staff_write_mode FROM store_metadata").fetchone()
            if metadata["staff_write_mode"] != "revision_required":
                raise StateConflict("品質確認には保護付き更新モードが必要です。")
            try:
                current = context(conn, rid)
            except LookupError:
                current = None
            error, status, grant = None, 200, None

            def reject(code, detail, http_status=409):
                return dict(code=code, detail=detail), http_status

            if command["write_epoch"] != metadata["write_epoch"]:
                error, status = reject("write_epoch_conflict", "書込み世代が変わりました。")
            elif current is None:
                error, status = reject("reception_not_found", "受付が見つかりません。", 404)
            elif current["revision"] != command["expected_revision"]:
                error, status = reject("revision_conflict", "受付が更新されています。")
            elif current["watermarks"] != command["expected_watermarks"]:
                error, status = reject("quality_context_conflict", "品質確認の対象範囲が更新されています。")
            if error is None and action == "quality_review":
                issue = next((item for item in current["issues"] if item["issue_id"] == command["issue_id"]), None)
                if issue is None:
                    error, status = reject("quality_issue_not_found", "対象受付の品質問題が見つかりません。", 404)
                elif (issue["latest_review"]["review_number"] if issue["latest_review"] else 0) != command["expected_review_number"]:
                    error, status = reject("quality_sequence_conflict", "品質レビューが更新されています。")
            elif error is None and current["latest_assessment_id"] != command["expected_assessment_id"]:
                error, status = reject("quality_sequence_conflict", "品質判定が更新されています。")
            if error is None and command["decision"] in ("clear", "eligible"):
                grant = authorize(self.authority, principal_kind, principal_id, command["decision"],
                                  command["evidence"], command, current["valid_seated_event_id"])
                if grant is None:
                    error, status = reject("quality_authorization_required", "根拠を確認できる独立した承認権限が必要です。", 403)
                elif command["decision"] == "eligible" and review_clear_reasons(
                        conn, rid, current["revision"], current["valid_seated_event_id"], current["watermarks"],
                        authority=self.authority, verify_authority=True):
                    error, status = reject("quality_not_eligible", "未解決または古い品質レビューがあります。")
            timestamp = self.store._now(conn, persist=True).isoformat()
            child = None
            if error is None:
                row = dict(reception_id=rid, operation_id=operation_id, decision=command["decision"],
                           reason_code=command["reason_code"], reason_detail=command["reason_detail"], recorded_at=timestamp)
                if action == "quality_review":
                    row.update(issue_id=command["issue_id"], review_number=command["expected_review_number"] + 1)
                    child = self._save_review(conn, row)
                else:
                    row.update(reception_revision=current["revision"], valid_seated_event_id=current["valid_seated_event_id"],
                               policy_version=POLICY_VERSION,
                               checked_through_event_id=current["watermarks"]["event_id"],
                               issues_through_id=current["watermarks"]["issue_id"],
                               reviews_through_id=current["watermarks"]["review_id"])
                    child = self._save_assessment(conn, row)
            outcome = "rejected" if error else ("review_added" if action == "quality_review" else "assessment_added")
            revision = current["revision"] if current else None
            response = dict(operation_id=operation_id, reception_id=rid, action=action, outcome=outcome,
                            response_status=status, recorded_at=timestamp, resulting_revision=revision,
                            quality_result=child, error=error)
            request_json = canonical_json(dict(command=command, authorization=grant))
            operation = dict(operation_id=operation_id, request_schema_version=1,
                             requested_reception_id=rid, resolved_reception_id=rid if current else None,
                             action=action, write_epoch=command["write_epoch"], expected_revision=expected_revision,
                             principal_kind=principal_kind, principal_id=principal_id, request_json=request_json,
                             request_sha256=sha256(request_json.encode()).hexdigest(), outcome=outcome,
                             response_status=status, response_json=canonical_json(response),
                             resulting_revision=revision, recorded_at=timestamp)
            self.store._saved_staff_response(operation)
            self.store._save_staff_operation(conn, operation)
            validate_history(conn, 2)
            return response
