"""Frozen schema from f470ddf; never imports or changes the application store."""

V1_SCHEMA = (
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
