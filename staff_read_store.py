"""Read-only staff operation context, bounded lists and snapshot-bound cursors.

Every response uses one SQLite read transaction. Cursor pages fail closed after
any reception event/configuration change; they never silently mix queue states.
"""

import base64
from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import json
import secrets
from time import monotonic

from reception_schema import ALLOWED_TRANSITIONS
from reception_store import ACTIVE_STATUSES, ReceptionNotFound, StateConflict, StoreValidationError
from staff_operation import ACTION_TARGETS, canonical_json


ROW_FIELDS = (
    "id", "display_number", "reception_order", "received_at", "adults", "children",
    "total_people", "status", "called_at", "seated_at", "cancelled_at", "actual_wait_minutes",
)


class StaffReadStore:
    def __init__(self, store, *, cursor_clock=monotonic):
        self.store = store
        self._cursor_key = secrets.token_bytes(32)
        self._cursor_clock = cursor_clock

    def _metadata(self, conn):
        version = self.store.schema_version
        if version == 2:
            row = conn.execute("SELECT database_id,write_epoch,staff_write_mode,seat_corrections_enabled "
                               "FROM store_metadata WHERE singleton=1").fetchone()
            result = dict(row)
            result["seat_corrections_enabled"] = bool(result["seat_corrections_enabled"])
        else:
            result = dict(database_id=None, write_epoch=None, staff_write_mode="legacy",
                          seat_corrections_enabled=False)
        result["schema_version"] = version
        result["write_enabled"] = version == 2 and result["staff_write_mode"] == "revision_required"
        result["disabled_reason"] = None if result["write_enabled"] else "protected_mode_required"
        event_id = conn.execute("SELECT COALESCE(MAX(event_id),0) FROM reception_events").fetchone()[0]
        result["view_version"] = sha256(canonical_json([result, event_id]).encode()).hexdigest()
        return result

    def _row(self, conn, row, metadata):
        result = {key: row[key] for key in ROW_FIELDS}
        if self.store.schema_version == 2:
            result["revision"] = self.store._checked_state(conn, row["id"])["revision"]
        else:
            result["revision"] = None
        actions = [action for action, target in ACTION_TARGETS.items()
                   if target in ALLOWED_TRANSITIONS[row["status"]]] if metadata["write_enabled"] else []
        event = conn.execute("SELECT * FROM reception_events WHERE reception_id=? "
                             "ORDER BY event_id DESC LIMIT 1", (row["id"],)).fetchone()
        seating = None
        if row["status"] == "seated":
            if (event is None or event["event_type"] != "seated" or event["to_status"] != "seated"
                    or event["from_status"] not in ACTIVE_STATUSES
                    or event["occurred_at"] != row["seated_at"]):
                raise StoreValidationError("Current seating event is inconsistent.")
            seating = {key: event[key] for key in ("event_id", "from_status", "to_status", "occurred_at")}
            seating["correctable"] = metadata["write_enabled"] and metadata["seat_corrections_enabled"]
            if seating["correctable"]:
                actions.append("seating_correction")
        result.update(allowed_actions=actions, valid_seating_event=seating)
        return result

    def detail(self, reception_id):
        with self.store._connection() as conn:
            metadata = self._metadata(conn)
            row = conn.execute("SELECT * FROM receptions WHERE id=?", (reception_id,)).fetchone()
            if row is None:
                raise ReceptionNotFound(reception_id)
            return dict(metadata=metadata, reception=self._row(conn, row, metadata))

    def configuration(self):
        with self.store._connection() as conn:
            return self._metadata(conn)

    def _encode(self, payload):
        raw = canonical_json(payload).encode()
        return base64.urlsafe_b64encode(raw + hmac.digest(self._cursor_key, raw, "sha256")).decode().rstrip("=")

    def _decode(self, cursor):
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                raise ValueError("Invalid cursor.")
            decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            raw, signature = decoded[:-32], decoded[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, raw, "sha256")):
                raise ValueError("Invalid signature.")
            payload = json.loads(raw)
            if payload["expires"] <= self._cursor_clock():
                raise ValueError("Expired cursor.")
            return payload
        except (ValueError, KeyError, TypeError) as exc:
            raise StoreValidationError("Invalid or expired staff cursor.") from exc

    def listing(self, *, view="active", limit=50, hours=24, cursor=None):
        if (view not in ("active", "recently_seated") or type(limit) is not int
                or not 1 <= limit <= 100 or type(hours) is not int or not 1 <= hours <= 168):
            raise StoreValidationError("Invalid staff list bounds.")
        with self.store._connection() as conn:
            metadata = self._metadata(conn)
            if cursor is None:
                as_of = self.store._now(conn).isoformat()
                last = 0 if view == "active" else None
                expires = self._cursor_clock() + 300
            else:
                page = self._decode(cursor)
                if [page["view"], page["limit"], page["hours"]] != [view, limit, hours]:
                    raise StoreValidationError("Cursor parameters changed.")
                if page["version"] != metadata["view_version"]:
                    raise StateConflict("Staff list changed; reload the first page.")
                as_of, last, expires = page["as_of"], page["last"], page["expires"]
            if view == "active":
                rows = conn.execute("SELECT * FROM receptions WHERE status IN ('waiting','called','absent') "
                                    "AND reception_order>? ORDER BY reception_order LIMIT ?",
                                    (last, limit + 1)).fetchall()
            else:
                since = (datetime.fromisoformat(as_of) - timedelta(hours=hours)).isoformat()
                # Latest per-reception event must still be a valid, current seating.
                rows = conn.execute("SELECT r.*, e.event_id AS page_key FROM receptions r "
                                    "JOIN reception_events e ON e.reception_id=r.id "
                                    "AND e.event_id=(SELECT MAX(event_id) FROM reception_events WHERE reception_id=r.id) "
                                    "WHERE r.status='seated' AND e.event_type='seated' AND (? IS NULL OR e.event_id<?) "
                                    "AND julianday(e.occurred_at)>=julianday(?) "
                                    "ORDER BY e.event_id DESC LIMIT ?", (last, last, since, limit + 1)).fetchall()
            selected, has_more = rows[:limit], len(rows) > limit
            records = [self._row(conn, row, metadata) for row in selected]
            next_cursor = None
            if has_more:
                last = selected[-1]["reception_order" if view == "active" else "page_key"]
                next_cursor = self._encode(dict(view=view, limit=limit, hours=hours, version=metadata["view_version"],
                                                as_of=as_of, last=last, expires=expires))
            return dict(metadata=metadata, view=view, as_of=as_of, receptions=records,
                        has_more=has_more, next_cursor=next_cursor)
