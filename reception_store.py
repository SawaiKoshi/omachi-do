"""Local reception records, immutable intake snapshots, and operation events.

Every write opens a separate SQLite connection and obtains BEGIN IMMEDIATE before
reading the queue or the system clock. No Excel files or training code are used.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from reception_schema import (
    ALLOWED_TRANSITIONS, STATE_FIELDS, SchemaValidationError, validate_history, validate_schema,
)
from staff_operation import (
    ACTION_TARGETS, REQUEST_VERSION, canonical_json, command_request, correction_request, receipt_response,
    saved_response, uuid_text, validate_principal,
)
from seat_correction_quality import expected_issues


JST = ZoneInfo("Asia/Tokyo")
SCHEMA_VERSION = 1  # New databases remain v1; existing v2 requires explicit migration.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
ACTIVE_STATUSES = ("waiting", "called", "absent")
_PREDICTION_STATUSES = {
    "available", "model_unavailable", "no_waiting_groups",
    "unsupported_weekday", "unsupported_time", "prediction_failed",
}
_TABLES = {
    "store_metadata", "daily_counters", "receptions", "reception_snapshots",
    "predictions", "reception_events", "idempotency_keys",
}


class StoreValidationError(ValueError):
    """Invalid people counts, clock values, or database schema."""


class ReceptionNotFound(LookupError):
    """The requested internal reception ID does not exist."""


class StateConflict(ValueError):
    """The requested reception state transition is not allowed."""


class IdempotencyConflict(ValueError):
    """An idempotency key was already used with different people counts."""


class StoreBusy(RuntimeError):
    """SQLite could not obtain its lock within the configured timeout."""


_SCHEMA = (
    """CREATE TABLE store_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        last_recorded_at TEXT
    )""",
    """CREATE TABLE daily_counters (
        reception_date TEXT PRIMARY KEY,
        last_number INTEGER NOT NULL CHECK (last_number > 0)
    )""",
    """CREATE TABLE receptions (
        reception_order INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        display_number TEXT NOT NULL UNIQUE,
        received_at TEXT NOT NULL CHECK (julianday(received_at) IS NOT NULL),
        adults INTEGER NOT NULL CHECK (typeof(adults) = 'integer' AND adults >= 0),
        children INTEGER NOT NULL CHECK (typeof(children) = 'integer' AND children >= 0),
        total_people INTEGER NOT NULL CHECK (
            typeof(total_people) = 'integer' AND total_people = adults + children
            AND total_people BETWEEN 1 AND 100),
        status TEXT NOT NULL CHECK (status IN
            ('waiting', 'called', 'absent', 'seated', 'cancelled')),
        called_at TEXT,
        seated_at TEXT,
        cancelled_at TEXT,
        actual_wait_minutes REAL,
        recorded_at TEXT NOT NULL CHECK (julianday(recorded_at) IS NOT NULL),
        source TEXT NOT NULL,
        CHECK (julianday(recorded_at) >= julianday(received_at)),
        CHECK (called_at IS NULL OR (julianday(called_at) IS NOT NULL
            AND julianday(called_at) >= julianday(received_at))),
        CHECK (seated_at IS NULL OR (julianday(seated_at) IS NOT NULL
            AND julianday(seated_at) >= julianday(received_at))),
        CHECK (cancelled_at IS NULL OR (julianday(cancelled_at) IS NOT NULL
            AND julianday(cancelled_at) >= julianday(received_at))),
        CHECK (
            (status = 'seated' AND seated_at IS NOT NULL AND cancelled_at IS NULL
                AND actual_wait_minutes IS NOT NULL AND actual_wait_minutes >= 0)
            OR (status = 'cancelled' AND cancelled_at IS NOT NULL
                AND seated_at IS NULL AND actual_wait_minutes IS NULL)
            OR (status IN ('waiting', 'called', 'absent') AND seated_at IS NULL
                AND cancelled_at IS NULL AND actual_wait_minutes IS NULL)),
        CHECK (status NOT IN ('called', 'absent') OR called_at IS NOT NULL)
    )""",
    """CREATE TABLE reception_snapshots (
        reception_id TEXT PRIMARY KEY REFERENCES receptions(id),
        captured_at TEXT NOT NULL CHECK (julianday(captured_at) IS NOT NULL),
        source TEXT NOT NULL,
        waiting_groups INTEGER NOT NULL CHECK (waiting_groups >= 0),
        ahead_groups INTEGER NOT NULL CHECK (ahead_groups >= 0 AND ahead_groups <= waiting_groups),
        ahead_same_size_groups INTEGER NOT NULL CHECK (
            ahead_same_size_groups >= 0 AND ahead_same_size_groups <= ahead_groups),
        groups_1_2 INTEGER NOT NULL CHECK (groups_1_2 >= 0),
        groups_3_4 INTEGER NOT NULL CHECK (groups_3_4 >= 0),
        groups_5_plus INTEGER NOT NULL CHECK (groups_5_plus >= 0),
        CHECK (waiting_groups = groups_1_2 + groups_3_4 + groups_5_plus)
    )""",
    """CREATE TABLE predictions (
        reception_id TEXT PRIMARY KEY REFERENCES receptions(id),
        predicted_at TEXT NOT NULL CHECK (julianday(predicted_at) IS NOT NULL),
        source TEXT NOT NULL,
        result_json TEXT NOT NULL CHECK (json_valid(result_json))
    )""",
    """CREATE TABLE reception_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reception_id TEXT NOT NULL REFERENCES receptions(id),
        event_type TEXT NOT NULL CHECK (event_type IN
            ('registered', 'waiting', 'called', 'absent', 'seated', 'cancelled')),
        from_status TEXT,
        to_status TEXT NOT NULL CHECK (to_status IN
            ('waiting', 'called', 'absent', 'seated', 'cancelled')),
        occurred_at TEXT NOT NULL CHECK (julianday(occurred_at) IS NOT NULL),
        recorded_at TEXT NOT NULL CHECK (julianday(recorded_at) IS NOT NULL
            AND julianday(recorded_at) >= julianday(occurred_at)),
        source TEXT NOT NULL
    )""",
    """CREATE TABLE idempotency_keys (
        key TEXT PRIMARY KEY,
        reception_id TEXT NOT NULL UNIQUE REFERENCES receptions(id),
        adults INTEGER NOT NULL,
        children INTEGER NOT NULL,
        response_json TEXT NOT NULL CHECK (json_valid(response_json))
    )""",
    "CREATE INDEX queue_status_order ON receptions(status, reception_order)",
    "CREATE INDEX events_reception ON reception_events(reception_id, event_id)",
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _validate_people(adults: int, children: int) -> None:
    if (type(adults) is not int or type(children) is not int
            or adults < 0 or children < 0 or not 1 <= adults + children <= 100):
        raise StoreValidationError("大人・子供は0人以上の整数で、合計1〜100人にしてください。")


def _size_band(total_people: int) -> str:
    return "1-2" if total_people <= 2 else "3-4" if total_people <= 4 else "5+"


class ReceptionStore:
    """A file-backed SQLite store, initialized and schema-checked on construction.

    Known v1 and migrated v2 schemas are strictly checked; neither is migrated.
    A store instance stays bound to its initially validated version. Optional
    expected_schema_version rejects accidental connections to another version.
    Timestamps are generated internally, stored with their JST offset, and never
    accepted as customer-supplied intake or seating times. Pending records remain
    active across midnight until a staff operation resolves them.
    """

    def __init__(self, db_path: str | Path, clock: Callable[[], datetime] | None = None,
                 *, expected_schema_version: int | None = None):
        if str(db_path) == ":memory:":
            raise StoreValidationError("独立した接続間で共有できるSQLiteファイルを指定してください。")
        if (expected_schema_version is not None and
                (type(expected_schema_version) is not int or expected_schema_version not in SUPPORTED_SCHEMA_VERSIONS)):
            raise StoreValidationError("対応するDBスキーマ版は1または2です。")
        self.db_path = Path(db_path).absolute()
        self._schema_version = expected_schema_version
        self._database_identity = None
        self.clock = clock if clock is not None else lambda: datetime.now(JST)
        self.initialize()

    @property
    def schema_version(self):
        return self._schema_version

    @contextmanager
    def _connection(self, *, write: bool = False):
        mode = "rw" if write else "ro"
        conn = sqlite3.connect(self.db_path.as_uri() + f"?mode={mode}", uri=True,
                               timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            # Check before any business DML, within the same transaction/lock.
            validate_schema(conn, self.schema_version, check_integrity=False,
                            allow_revision_required=True)
            if self.schema_version == 2:
                identity = tuple(conn.execute("SELECT database_id,write_epoch FROM store_metadata").fetchone())
                if identity != self._database_identity:
                    raise SchemaValidationError("Database identity or write epoch changed; reopen the store.")
            yield conn
            conn.commit()
        except SchemaValidationError as exc:
            conn.rollback()
            raise StoreValidationError("DBスキーマまたは設定が不正です。自動修復は行いません。") from exc
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StoreBusy("受付DBが使用中です。同じ送信キーで再試行してください。") from exc
            raise
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        new_file = not self.db_path.exists()
        if not new_file and self.db_path.stat().st_size == 0:
            raise StoreValidationError("既存DBが空です。自動初期化は行いません。")
        if new_file and self.schema_version == 2:
            raise StoreValidationError("v2の新規作成・自動移行は行いません。移行済みDBを指定してください。")
        if new_file:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "rwc" if new_file else "ro"
        conn = sqlite3.connect(self.db_path.as_uri() + f"?mode={mode}", uri=True,
                               timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE" if new_file else "BEGIN")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if new_file and version == 0:
                if conn.execute("SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise SchemaValidationError("Unknown unversioned database.")
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute("INSERT INTO store_metadata(singleton) VALUES (1)")
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                version = SCHEMA_VERSION
            expected = self.schema_version if self.schema_version is not None else version
            validate_schema(conn, expected, allow_revision_required=True)
            validate_history(conn, expected)
            identity = tuple(conn.execute("SELECT database_id,write_epoch FROM store_metadata").fetchone()) if expected == 2 else None
            if self._database_identity is not None and identity != self._database_identity:
                raise SchemaValidationError("Database identity or write epoch changed; recreate the store.")
            if new_file:
                conn.commit()
            else:
                conn.rollback()
            self._schema_version = expected
            self._database_identity = identity
        except SchemaValidationError as exc:
            conn.rollback()
            raise StoreValidationError("未対応または不整合のDBです。自動移行・自動修復は行いません。") from exc
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _now(self, conn: sqlite3.Connection, *, persist: bool = False) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise StoreValidationError("システム時刻にはタイムゾーン付きdatetimeが必要です。")
        now = value.astimezone(JST)
        previous = conn.execute(
            "SELECT last_recorded_at FROM store_metadata WHERE singleton = 1"
        ).fetchone()[0]
        if previous is not None and now < datetime.fromisoformat(previous):
            raise StoreValidationError("システム時刻が前回の記録より前です。時刻設定を確認してください。")
        if persist:
            conn.execute("UPDATE store_metadata SET last_recorded_at = ? WHERE singleton = 1",
                         (now.isoformat(),))
        return now

    @staticmethod
    def _snapshot(conn: sqlite3.Connection, total_people: int, captured_at: datetime) -> dict:
        # One query under one transaction provides a coherent, self-excluding view.
        rows = conn.execute(
            "SELECT total_people FROM receptions WHERE status IN ('waiting', 'called', 'absent')"
        )
        bands = {"1-2": 0, "3-4": 0, "5+": 0}
        for row in rows:
            bands[_size_band(row[0])] += 1
        total = sum(bands.values())
        return {
            "captured_at": captured_at.isoformat(), "source": "reception_database",
            "waiting_groups": total, "ahead_groups": total,
            "ahead_same_size_groups": bands[_size_band(total_people)],
            "party_size_groups": bands,
        }

    def snapshot(self, total_people: int) -> dict:
        """Read the queue for a hypothetical new arrival without saving anything."""
        if type(total_people) is not int or not 1 <= total_people <= 100:
            raise StoreValidationError("合計人数は1〜100の整数にしてください。")
        with self._connection() as conn:
            return self._snapshot(conn, total_people, self._now(conn))

    @staticmethod
    def _prediction(predictor, adults, children, received_at, waiting_groups) -> dict:
        fallback = {
            "status": "model_unavailable" if predictor is None else "prediction_failed",
            "minutes": None, "model_version": None,
            "predicted_at": received_at.isoformat(),
            "warnings": ["予測モデルを利用できません。待ち時間はスタッフに確認してください。"],
        }
        if predictor is None:
            return fallback
        try:
            result = predictor(adults=adults, children=children,
                               received_at=received_at, waiting_groups=waiting_groups)
            required = {"status", "minutes", "model_version", "predicted_at", "warnings"}
            if not isinstance(result, dict) or not required.issubset(result):
                return fallback
            if not isinstance(result["status"], str) or result["status"] not in _PREDICTION_STATUSES:
                return fallback
            minutes = result["minutes"]
            if minutes is not None and (
                type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes < 0
            ):
                return fallback
            version = result["model_version"]
            if version is not None and (not isinstance(version, str) or not version.strip()):
                return fallback
            if result["status"] == "available" and (minutes is None or version is None):
                return fallback
            if result["status"] != "available" and minutes is not None:
                return fallback
            if not isinstance(result["predicted_at"], str):
                return fallback
            predicted_at = datetime.fromisoformat(result["predicted_at"])
            if predicted_at.tzinfo is None or predicted_at.utcoffset() is None:
                return fallback
            if not isinstance(result["warnings"], list) or any(
                not isinstance(warning, str) for warning in result["warnings"]
            ):
                return fallback
            # Freeze the returned data and reject NaN/non-JSON results before writing.
            return json.loads(_json(result))
        except Exception:
            # Operational intake must succeed even if the local model fails.
            return fallback

    def register(self, adults: int, children: int, idempotency_key: str, *, predictor=None):
        """Atomically number, snapshot, predict, and register a new reception.

        Retrying the same key and counts returns the original registration
        response, even if staff have since changed the reception's current state.
        """
        _validate_people(adults, children)
        try:
            if not isinstance(idempotency_key, str):
                raise ValueError
            key = str(UUID(idempotency_key))
        except (ValueError, AttributeError) as exc:
            raise StoreValidationError("送信キーにはUUIDを指定してください。") from exc
        with self._connection(write=True) as conn:
            saved = conn.execute("SELECT * FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
            if saved is not None:
                if (saved["adults"], saved["children"]) != (adults, children):
                    raise IdempotencyConflict("同じ送信キーが異なる人数で使われています。")
                return json.loads(saved["response_json"]), False
            now = self._now(conn, persist=True)
            timestamp = now.isoformat()
            snap = self._snapshot(conn, adults + children, now)
            day = now.strftime("%Y%m%d")
            conn.execute(
                "INSERT INTO daily_counters VALUES (?, 1) ON CONFLICT(reception_date) "
                "DO UPDATE SET last_number = last_number + 1", (day,)
            )
            number = conn.execute(
                "SELECT last_number FROM daily_counters WHERE reception_date = ?", (day,)
            ).fetchone()[0]
            reception_id = str(uuid4())
            conn.execute(
                "INSERT INTO receptions(id, display_number, received_at, adults, children, "
                "total_people, status, recorded_at, source) VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?, ?)",
                (reception_id, f"{day}-{number:03d}", timestamp, adults, children,
                 adults + children, timestamp, "reception_api"),
            )
            bands = snap["party_size_groups"]
            conn.execute("INSERT INTO reception_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (reception_id, timestamp, snap["source"], snap["waiting_groups"],
                          snap["ahead_groups"], snap["ahead_same_size_groups"],
                          bands["1-2"], bands["3-4"], bands["5+"]))
            prediction = self._prediction(predictor, adults, children, now, snap["waiting_groups"])
            conn.execute("INSERT INTO predictions VALUES (?, ?, ?, ?)",
                         (reception_id, timestamp, "local_prediction_service", _json(prediction)))
            self._append_event(conn, reception_id, "registered", None, "waiting", timestamp, "reception_api")
            response = self._get(conn, reception_id)
            conn.execute("INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?)",
                         (key, reception_id, adults, children, _json(response)))
            return response, True

    @staticmethod
    def _get(conn: sqlite3.Connection, reception_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM receptions WHERE id = ?", (reception_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        # Internal concurrency metadata must not change the original receipt or
        # the initial idempotent response, even before HTTP response filtering.
        result.pop("revision", None)
        snap = conn.execute("SELECT * FROM reception_snapshots WHERE reception_id = ?",
                            (reception_id,)).fetchone()
        result["snapshot"] = {
            "captured_at": snap["captured_at"], "source": snap["source"],
            "waiting_groups": snap["waiting_groups"], "ahead_groups": snap["ahead_groups"],
            "ahead_same_size_groups": snap["ahead_same_size_groups"],
            "party_size_groups": {"1-2": snap["groups_1_2"], "3-4": snap["groups_3_4"],
                                  "5+": snap["groups_5_plus"]},
        }
        prediction = conn.execute("SELECT result_json FROM predictions WHERE reception_id = ?",
                                  (reception_id,)).fetchone()
        result["prediction"] = json.loads(prediction[0])
        return result

    def get(self, reception_id: str) -> dict | None:
        with self._connection() as conn:
            return self._get(conn, reception_id)

    def get_queue(self) -> list[dict]:
        with self._connection() as conn:
            ids = conn.execute(
                "SELECT id FROM receptions WHERE status IN ('waiting', 'called', 'absent') "
                "ORDER BY reception_order"
            ).fetchall()
            return [self._get(conn, row[0]) for row in ids]

    def get_events(self, reception_id: str) -> list[dict]:
        with self._connection() as conn:
            if self._get(conn, reception_id) is None:
                raise ReceptionNotFound(reception_id)
            return [dict(row) for row in conn.execute(
                "SELECT * FROM reception_events WHERE reception_id = ? ORDER BY event_id",
                (reception_id,),
            )]

    @staticmethod
    def _audit_state(conn, reception_id: str) -> dict:
        row = conn.execute("SELECT " + ", ".join(STATE_FIELDS) + " FROM receptions WHERE id = ?",
                           (reception_id,)).fetchone()
        return dict(row)

    def _append_event(self, conn, reception_id, kind, previous, target, timestamp, source, *, before=None,
                      operation_id=None):
        if self.schema_version == 1:
            conn.execute(
                "INSERT INTO reception_events(reception_id, event_type, from_status, to_status, "
                "occurred_at, recorded_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (reception_id, kind, previous, target, timestamp, timestamp, source),
            )
        else:
            after = self._audit_state(conn, reception_id)
            conn.execute(
                "INSERT INTO reception_events(reception_id, event_type, from_status, to_status, "
                "occurred_at, recorded_at, source, audit_kind, revision_before, revision_after, "
                "before_state_json, after_state_json, operation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (reception_id, kind, previous, target, timestamp, timestamp, source,
                 "registration_v2" if kind == "registered" else ("staff_v2" if operation_id else "staff_legacy_v2"),
                 before["revision"] if before is not None else None, after["revision"],
                 _json(before) if before is not None else None, _json(after), operation_id),
            )

    def _checked_state(self, conn, reception_id):
        before = self._audit_state(conn, reception_id)
        event = conn.execute("SELECT to_status,revision_after,after_state_json FROM reception_events "
                             "WHERE reception_id=? ORDER BY event_id DESC LIMIT 1", (reception_id,)).fetchone()
        if (event is None or event["to_status"] != before["status"]
                or (event["revision_after"] or 0) != before["revision"]
                or (event["after_state_json"] is not None
                    and json.loads(event["after_state_json"]) != before)):
            raise StoreValidationError("現在状態と監査履歴が一致しません。")
        return before

    def _transition_record(self, conn, record, target, before, timestamp, *, operation_id=None):
        # Shared by legacy/protected paths: intake time, snapshots and predictions
        # never change. Calling is not actual seating.
        reception_id = record["id"]
        now = datetime.fromisoformat(timestamp)
        received = datetime.fromisoformat(record["received_at"])
        if now < received:
            raise StoreValidationError("操作時刻が受付時刻より前です。")
        called_at = timestamp if target == "called" else record["called_at"]
        seated_at = timestamp if target == "seated" else None
        cancelled_at = timestamp if target == "cancelled" else None
        wait = (now - received).total_seconds() / 60 if target == "seated" else None
        revision_sql = ", revision = revision + 1" if before is not None else ""
        revision_where = " AND revision = ?" if before is not None else ""
        parameters = (target, called_at, seated_at, cancelled_at, wait, reception_id)
        if before is not None:
            parameters += (before["revision"],)
        updated = conn.execute(
            "UPDATE receptions SET status = ?, called_at = ?, seated_at = ?, cancelled_at = ?, "
            "actual_wait_minutes = ?" + revision_sql + " WHERE id = ?" + revision_where,
            parameters,
        )
        if updated.rowcount != 1:
            raise StateConflict("受付の更新版が変わりました。")
        arguments = dict(before=before)
        if operation_id is not None:
            arguments["operation_id"] = operation_id
        self._append_event(conn, reception_id, target, record["status"], target, timestamp,
                           "staff_operation", **arguments)
        return self._get(conn, reception_id)

    def transition(self, reception_id: str, target: str) -> dict:
        """Record a staff action; calling is never treated as actual seating."""
        with self._connection(write=True) as conn:
            if self.schema_version == 2:
                mode = conn.execute("SELECT staff_write_mode FROM store_metadata WHERE singleton = 1").fetchone()[0]
                if mode != "legacy":
                    # Check even for no-ops, before changing clocks or any rows.
                    raise StateConflict("操作IDと期待revisionに対応していない更新経路は利用できません。")
            record = self._get(conn, reception_id)
            if record is None:
                raise ReceptionNotFound(reception_id)
            previous = record["status"]
            before = self._checked_state(conn, reception_id) if self.schema_version == 2 else None
            if target == previous:
                return record
            if target not in ALLOWED_TRANSITIONS[previous]:
                raise StateConflict(f"{previous} から {target} への状態変更はできません。")
            return self._transition_record(conn, record, target, before, self._now(conn, persist=True).isoformat())

    def _require_staff_v2(self):
        if self.schema_version != 2:
            raise StateConflict("保護付きスタッフ操作には明示的に移行したv2 DBが必要です。")

    def get_staff_reception(self, reception_id):
        self._require_staff_v2()
        with self._connection() as conn:
            record = self._get(conn, reception_id)
            if record is None:
                raise ReceptionNotFound(reception_id)
            before = self._checked_state(conn, reception_id)
            metadata = conn.execute("SELECT write_epoch,staff_write_mode FROM store_metadata").fetchone()
            return dict(reception=receipt_response(record), revision=before["revision"],
                        write_epoch=metadata["write_epoch"], staff_write_mode=metadata["staff_write_mode"])

    @staticmethod
    def _saved_staff_response(operation):
        try:
            return saved_response(operation)
        except (ValueError, TypeError, KeyError) as exc:
            raise StoreValidationError("スタッフ操作記録が不整合です。") from exc

    def get_staff_operation(self, operation_id, *, principal_kind, principal_id):
        self._require_staff_v2()
        try:
            operation_id = uuid_text(operation_id)
            validate_principal(principal_kind, principal_id)
        except (ValueError, TypeError) as exc:
            raise StoreValidationError("操作IDまたは認証主体が不正です。") from exc
        with self._connection() as conn:
            operation = conn.execute("SELECT * FROM staff_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if (operation is None or operation["principal_kind"] != principal_kind
                    or operation["principal_id"] != principal_id):
                raise ReceptionNotFound(operation_id)
            return self._saved_staff_response(operation)

    @staticmethod
    def _save_staff_operation(conn, operation):
        conn.execute("INSERT INTO staff_operations(" + ",".join(operation) + ") VALUES(" +
                     ",".join("?" for _ in operation) + ")", tuple(operation.values()))

    def staff_transition(self, reception_id, action, *, operation_id, expected_revision,
                         write_epoch, principal_kind, principal_id):
        """Atomically claim a command and archive its result, including business rejections.

        An existing command is replayed before checking today's revision/epoch.
        Its archived result describes that command, not the current reception.
        Credentials must never be supplied as principal IDs by transport adapters.
        """
        self._require_staff_v2()
        try:
            operation_id = uuid_text(operation_id)
            command = command_request(reception_id, action, expected_revision, write_epoch)
            validate_principal(principal_kind, principal_id)
        except (ValueError, TypeError) as exc:
            raise StoreValidationError("スタッフ操作の要求が不正です。") from exc
        reception_id, write_epoch = command["reception_id"], command["write_epoch"]
        request_json = canonical_json(command)
        digest = sha256(request_json.encode()).hexdigest()
        with self._connection(write=True) as conn:
            existing = conn.execute("SELECT * FROM staff_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if existing is not None:
                if (existing["principal_kind"] != principal_kind or existing["principal_id"] != principal_id
                        or existing["request_json"] != request_json or existing["request_sha256"] != digest):
                    raise StateConflict("操作IDが別の要求または認証主体で使用されています。")
                return self._saved_staff_response(existing)
            metadata = conn.execute("SELECT write_epoch FROM store_metadata").fetchone()
            record = self._get(conn, reception_id)
            before = self._checked_state(conn, reception_id) if record is not None else None
            error, status = None, 200
            # Epoch validation also applies to commands for a missing reception.
            if write_epoch != metadata["write_epoch"]:
                error, status = dict(code="write_epoch_conflict", detail="書込み世代が変わりました。最新情報を取得してください。"), 409
            elif record is None:
                error, status = dict(code="reception_not_found", detail="受付が見つかりません。"), 404
            elif before["revision"] != expected_revision:
                error, status = dict(code="revision_conflict", detail="受付が更新されています。最新情報を取得してください。"), 409
            target = ACTION_TARGETS[action]
            if error is None and record["status"] != target and target not in ALLOWED_TRANSITIONS[record["status"]]:
                error, status = dict(code="state_conflict", detail="この状態から操作できません。"), 409
            timestamp = self._now(conn, persist=True).isoformat()
            outcome = "rejected" if error else "no_change"
            if error is None and record["status"] != target:
                record = self._transition_record(conn, record, target, before, timestamp, operation_id=operation_id)
                outcome = "state_changed"
            revision = None if before is None else before["revision"] + (outcome == "state_changed")
            response = dict(operation_id=operation_id, reception_id=reception_id, action=action,
                            outcome=outcome, response_status=status, recorded_at=timestamp,
                            resulting_revision=revision, reception=receipt_response(record) if not error else None,
                            error=error)
            operation = dict(operation_id=operation_id, request_schema_version=REQUEST_VERSION,
                             requested_reception_id=reception_id,
                             resolved_reception_id=reception_id if record is not None else None,
                             action=action, write_epoch=write_epoch, expected_revision=expected_revision,
                             principal_kind=principal_kind, principal_id=principal_id,
                             request_json=request_json, request_sha256=digest, outcome=outcome,
                             response_status=status, response_json=canonical_json(response),
                             resulting_revision=revision, recorded_at=timestamp)
            self._saved_staff_response(operation)
            self._save_staff_operation(conn, operation)
            return response

    def _append_correction_event(self, conn, reception_id, before, target_event, timestamp,
                                 operation_id, reason_code, reason_detail):
        after = self._audit_state(conn, reception_id)
        cursor = conn.execute(
            "INSERT INTO reception_events(reception_id,event_type,from_status,to_status,occurred_at,"
            "recorded_at,source,audit_kind,operation_id,revision_before,revision_after,corrects_event_id,"
            "reason_code,reason_detail,before_state_json,after_state_json) "
            "VALUES (?,'seating_corrected','seated',?,?,?,'staff_operation','staff_v2',?,?,?,?,?,?,?,?)",
            (reception_id, after["status"], timestamp, timestamp, operation_id, before["revision"],
             after["revision"], target_event["event_id"], reason_code, reason_detail, _json(before), _json(after)),
        )
        return dict(conn.execute("SELECT * FROM reception_events WHERE event_id=?", (cursor.lastrowid,)).fetchone())

    @staticmethod
    def _save_quality_issues(conn, issues):
        for issue in issues:
            conn.execute("INSERT INTO data_quality_issues(" + ",".join(issue) + ") VALUES(" +
                         ",".join("?" for _ in issue) + ")", tuple(issue.values()))

    def correct_seating(self, reception_id, *, operation_id, expected_revision, write_epoch,
                        target_event_id, reason_code, reason_detail, principal_kind, principal_id):
        """Invalidate exactly the current seating, preserving intake and all history.

        This never grants training eligibility. The entire impact evidence set
        must pass history validation before the transaction can be committed.
        """
        self._require_staff_v2()
        try:
            operation_id = uuid_text(operation_id)
            command = correction_request(reception_id, expected_revision, write_epoch,
                                         target_event_id, reason_code, reason_detail)
            validate_principal(principal_kind, principal_id)
        except (ValueError, TypeError) as exc:
            raise StoreValidationError("訂正要求が不正です。") from exc
        reception_id, write_epoch = command["reception_id"], command["write_epoch"]
        request_json = canonical_json(command)
        digest = sha256(request_json.encode()).hexdigest()
        with self._connection(write=True) as conn:
            existing = conn.execute("SELECT * FROM staff_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if existing is not None:
                if (existing["principal_kind"] != principal_kind or existing["principal_id"] != principal_id
                        or existing["request_json"] != request_json or existing["request_sha256"] != digest):
                    raise StateConflict("操作IDが別の要求または認証主体で使用されています。")
                return self._saved_staff_response(existing)
            metadata = conn.execute("SELECT write_epoch,staff_write_mode,seat_corrections_enabled FROM store_metadata").fetchone()
            if metadata["staff_write_mode"] != "revision_required" or metadata["seat_corrections_enabled"] != 1:
                raise StateConflict("保護モードと明示的な訂正有効化が必要です。")
            # Safety takes precedence over correction throughput: validate both
            # the pre-existing chain and the complete transaction before COMMIT.
            validate_history(conn, 2)
            record = self._get(conn, reception_id)
            before = self._checked_state(conn, reception_id) if record is not None else None
            target = conn.execute("SELECT * FROM reception_events WHERE event_id=?", (target_event_id,)).fetchone()
            latest = conn.execute("SELECT MAX(event_id) FROM reception_events WHERE reception_id=?", (reception_id,)).fetchone()[0]
            error, status = None, 200
            if write_epoch != metadata["write_epoch"]:
                error, status = dict(code="write_epoch_conflict", detail="書込み世代が変わりました。最新情報を取得してください。"), 409
            elif record is None:
                error, status = dict(code="reception_not_found", detail="受付が見つかりません。"), 404
            elif before["revision"] != expected_revision:
                error, status = dict(code="revision_conflict", detail="受付が更新されています。最新情報を取得してください。"), 409
            elif (record["status"] != "seated" or target is None
                  or target["reception_id"] != reception_id or target["event_type"] != "seated"
                  or target["from_status"] not in ACTIVE_STATUSES or target_event_id != latest
                  or conn.execute("SELECT 1 FROM reception_events WHERE corrects_event_id=?", (target_event_id,)).fetchone()):
                error, status = dict(code="correction_target_conflict", detail="現在有効な案内完了イベントではありません。"), 409
            timestamp = self._now(conn, persist=True).isoformat()
            if error is None:
                updated = conn.execute(
                    "UPDATE receptions SET status=?,seated_at=NULL,actual_wait_minutes=NULL,"
                    "cancelled_at=NULL,revision=revision+1 WHERE id=? AND revision=?",
                    (target["from_status"], reception_id, expected_revision),
                )
                if updated.rowcount != 1:
                    raise StateConflict("受付の更新版が変わりました。")
                correction = self._append_correction_event(conn, reception_id, before, target, timestamp,
                                                           operation_id, reason_code, reason_detail)
                events = [dict(row) for row in conn.execute("SELECT * FROM reception_events ORDER BY event_id")]
                self._save_quality_issues(conn, expected_issues(events, target, correction))
                record = self._get(conn, reception_id)
            outcome = "rejected" if error else "state_changed"
            revision = None if before is None else before["revision"] + (error is None)
            response = dict(operation_id=operation_id, reception_id=reception_id, action="seating_correction",
                            outcome=outcome, response_status=status, recorded_at=timestamp,
                            resulting_revision=revision, reception=receipt_response(record) if not error else None,
                            error=error)
            operation = dict(operation_id=operation_id, request_schema_version=REQUEST_VERSION,
                             requested_reception_id=reception_id,
                             resolved_reception_id=reception_id if record is not None else None,
                             action="seating_correction", write_epoch=write_epoch, expected_revision=expected_revision,
                             principal_kind=principal_kind, principal_id=principal_id,
                             request_json=request_json, request_sha256=digest, outcome=outcome,
                             response_status=status, response_json=canonical_json(response),
                             resulting_revision=revision, recorded_at=timestamp)
            self._saved_staff_response(operation)
            self._save_staff_operation(conn, operation)
            validate_history(conn, 2)
            return response

    def get_data_quality(self, reception_id):
        """Inspection only: no review API or approved SQLite training export exists."""
        self._require_staff_v2()
        with self._connection() as conn:
            if self._get(conn, reception_id) is None:
                raise ReceptionNotFound(reception_id)
            issues = [dict(row) for row in conn.execute(
                "SELECT * FROM data_quality_issues WHERE reception_id=? ORDER BY issue_id", (reception_id,))]
            return dict(reception_id=reception_id, training_use="blocked", issues=issues,
                        unresolved_issue_ids=[issue["issue_id"] for issue in issues],
                        reasons=["sqlite_training_export_not_implemented", "quality_review_not_implemented"]
                        + (["unresolved_quality_issues"] if issues else ["not_assessed"]))

    def get_staff_events(self, reception_id):
        self._require_staff_v2()
        return self.get_events(reception_id)
