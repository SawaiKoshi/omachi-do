# Frozen runtime reference from f470ddf. Test-only; never opened against real DBs.
"""Local reception records, immutable intake snapshots, and operation events.

Every write opens a separate SQLite connection and obtains BEGIN IMMEDIATE before
reading the queue or the system clock. No Excel files or training code are used.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
SCHEMA_VERSION = 1
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

    Version 1 databases are reopened; other/nonempty unknown schemas are rejected.
    Timestamps are generated internally, stored with their JST offset, and never
    accepted as customer-supplied intake or seating times. Pending records remain
    active across midnight until a staff operation resolves them.
    """

    def __init__(self, db_path: str | Path, clock: Callable[[], datetime] | None = None):
        if str(db_path) == ":memory:":
            raise StoreValidationError("独立した接続間で共有できるSQLiteファイルを指定してください。")
        self.db_path = Path(db_path)
        self.clock = clock if clock is not None else lambda: datetime.now(JST)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self, *, write: bool = False):
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
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
        with self._connection(write=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if not tables and version == 0:
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute("INSERT INTO store_metadata(singleton) VALUES (1)")
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION or tables != _TABLES:
                raise StoreValidationError("未対応のDBスキーマです。自動移行は行いません。")

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
            conn.execute(
                "INSERT INTO reception_events(reception_id, event_type, from_status, to_status, "
                "occurred_at, recorded_at, source) VALUES (?, 'registered', NULL, 'waiting', ?, ?, ?)",
                (reception_id, timestamp, timestamp, "reception_api"),
            )
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

    def transition(self, reception_id: str, target: str) -> dict:
        """Record a staff action; calling is never treated as actual seating."""
        allowed = {
            "waiting": {"called", "seated", "cancelled"},
            "called": {"absent", "seated", "cancelled"},
            "absent": {"waiting", "called", "seated", "cancelled"},
            "seated": set(), "cancelled": set(),
        }
        with self._connection(write=True) as conn:
            record = self._get(conn, reception_id)
            if record is None:
                raise ReceptionNotFound(reception_id)
            previous = record["status"]
            if target == previous:
                return record
            if target not in allowed[previous]:
                raise StateConflict(f"{previous} から {target} への状態変更はできません。")
            now = self._now(conn, persist=True)
            received = datetime.fromisoformat(record["received_at"])
            if now < received:
                raise StoreValidationError("操作時刻が受付時刻より前です。")
            timestamp = now.isoformat()
            called_at = timestamp if target == "called" else record["called_at"]
            seated_at = timestamp if target == "seated" else None
            cancelled_at = timestamp if target == "cancelled" else None
            wait = (now - received).total_seconds() / 60 if target == "seated" else None
            conn.execute(
                "UPDATE receptions SET status = ?, called_at = ?, seated_at = ?, cancelled_at = ?, "
                "actual_wait_minutes = ? WHERE id = ?",
                (target, called_at, seated_at, cancelled_at, wait, reception_id),
            )
            conn.execute(
                "INSERT INTO reception_events(reception_id, event_type, from_status, to_status, "
                "occurred_at, recorded_at, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (reception_id, target, previous, target, timestamp, timestamp, "staff_operation"),
            )
            return self._get(conn, reception_id)
