"""v2 DDL for the synthetic migration laboratory, not the application's schema."""

from functools import lru_cache
import re
import sqlite3

from .v1_schema import V1_SCHEMA


REVISION_SQL = """ALTER TABLE receptions ADD COLUMN revision INTEGER NOT NULL DEFAULT 0
    CHECK (typeof(revision) = 'integer' AND revision >= 0)"""


def integer(name, *, minimum=0, nullable=False):
    null = '' if nullable else ' NOT NULL'
    test = f"typeof({name})='integer' AND {name}>={minimum}"
    if nullable:
        test = f'{name} IS NULL OR ({test})'
    return f'{name} INTEGER{null} CHECK ({test})'


def timestamp(name):
    return f'{name} TEXT NOT NULL CHECK (julianday({name}) IS NOT NULL)'


def uuid_column(name):
    return f"""{name} TEXT NOT NULL CHECK (length({name})=36
        AND {name} GLOB '????????-????-????-????-????????????'
        AND {name} NOT GLOB '*[^0-9a-f-]*' AND length(replace({name},'-',''))=32
        AND {name}<>'00000000-0000-0000-0000-000000000000')"""


def document(name, *, nullable=False):
    null = '' if nullable else ' NOT NULL'
    test = f"json_valid({name}) AND json_type({name})='object'"
    if nullable:
        test = f'{name} IS NULL OR ({test})'
    return f'{name} TEXT{null} CHECK ({test})'


METADATA_SQL = f"""CREATE TABLE new_store_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1), last_recorded_at TEXT,
    {uuid_column('database_id')}, {uuid_column('write_epoch')},
    staff_write_mode TEXT NOT NULL DEFAULT 'legacy'
        CHECK (staff_write_mode IN ('legacy','revision_required')),
    seat_corrections_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(seat_corrections_enabled)='integer' AND seat_corrections_enabled IN (0,1)),
    CHECK (seat_corrections_enabled=0 OR staff_write_mode='revision_required')
)"""

EVENTS_SQL = f"""CREATE TABLE new_reception_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reception_id TEXT NOT NULL REFERENCES receptions(id),
    event_type TEXT NOT NULL CHECK (event_type IN
        ('registered','waiting','called','absent','seated','cancelled','seating_corrected')),
    from_status TEXT,
    to_status TEXT NOT NULL CHECK (to_status IN ('waiting','called','absent','seated','cancelled')),
    {timestamp('occurred_at')},
    recorded_at TEXT NOT NULL CHECK (julianday(recorded_at) IS NOT NULL
        AND julianday(recorded_at)>=julianday(occurred_at)),
    source TEXT NOT NULL,
    audit_kind TEXT NOT NULL CHECK (audit_kind IN
        ('legacy_v1','registration_v2','staff_legacy_v2','staff_v2')),
    operation_id TEXT,
    {integer('revision_before', nullable=True)},
    {integer('revision_after', nullable=True)},
    corrects_event_id INTEGER,
    reason_code TEXT CHECK (reason_code IS NULL OR reason_code IN
        ('wrong_reception','accidental_seating','other')),
    reason_detail TEXT CHECK (reason_detail IS NULL OR length(reason_detail)<=500),
    {document('before_state_json', nullable=True)},
    {document('after_state_json', nullable=True)},
    UNIQUE(event_id,reception_id),
    FOREIGN KEY(corrects_event_id,reception_id)
        REFERENCES new_reception_events(event_id,reception_id),
    FOREIGN KEY(operation_id,reception_id)
        REFERENCES staff_operations(operation_id,resolved_reception_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (audit_kind='legacy_v1' AND event_type<>'seating_corrected'
            AND operation_id IS NULL AND revision_before IS NULL AND revision_after IS NULL
            AND before_state_json IS NULL AND after_state_json IS NULL)
        OR (audit_kind='registration_v2' AND event_type='registered'
            AND from_status IS NULL AND to_status='waiting' AND operation_id IS NULL
            AND revision_before IS NULL AND revision_after IS NOT NULL AND revision_after=0
            AND before_state_json IS NULL AND after_state_json IS NOT NULL)
        OR (audit_kind IN ('staff_legacy_v2','staff_v2') AND event_type<>'registered'
            AND from_status IS NOT NULL AND from_status<>to_status
            AND revision_before IS NOT NULL AND revision_after IS NOT NULL
            AND revision_after=revision_before+1
            AND before_state_json IS NOT NULL AND after_state_json IS NOT NULL
            AND ((audit_kind='staff_legacy_v2' AND operation_id IS NULL)
                OR (audit_kind='staff_v2' AND operation_id IS NOT NULL)))
    ),
    CHECK (
        (event_type='seating_corrected' AND audit_kind='staff_v2'
            AND from_status='seated' AND to_status IN ('waiting','called','absent')
            AND corrects_event_id IS NOT NULL AND corrects_event_id<event_id
            AND reason_code IS NOT NULL
            AND (reason_code<>'other' OR (reason_detail IS NOT NULL AND length(trim(reason_detail))>0)))
        OR (event_type<>'seating_corrected' AND corrects_event_id IS NULL
            AND reason_code IS NULL AND reason_detail IS NULL)
    ),
    CHECK (audit_kind NOT IN ('staff_legacy_v2','staff_v2') OR event_type='seating_corrected'
        OR (event_type=to_status AND (
            (from_status='waiting' AND to_status IN ('called','seated','cancelled'))
            OR (from_status='called' AND to_status IN ('absent','seated','cancelled'))
            OR (from_status='absent' AND to_status IN ('waiting','called','seated','cancelled')))))
)"""

AUXILIARY_SQL = (
    f"""CREATE TABLE staff_operations (
        {uuid_column('operation_id')} PRIMARY KEY,
        {integer('request_schema_version', minimum=1)},
        requested_reception_id TEXT NOT NULL,
        resolved_reception_id TEXT REFERENCES receptions(id),
        action TEXT NOT NULL CHECK (action IN
            ('call','absent','return','seat','cancel','seating_correction','quality_review','quality_assessment')),
        {uuid_column('write_epoch')},
        {integer('expected_revision')},
        principal_kind TEXT NOT NULL CHECK (principal_kind IN ('shared_staff_token','staff_account','system')),
        principal_id TEXT NOT NULL CHECK (length(trim(principal_id)) BETWEEN 1 AND 128),
        {document('request_json')},
        request_sha256 TEXT NOT NULL CHECK (length(request_sha256)=64
            AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
        outcome TEXT NOT NULL CHECK (outcome IN
            ('state_changed','no_change','review_added','assessment_added','rejected')),
        response_status INTEGER NOT NULL CHECK (typeof(response_status)='integer'
            AND response_status BETWEEN 200 AND 599),
        {document('response_json')},
        {integer('resulting_revision', nullable=True)},
        {timestamp('recorded_at')},
        UNIQUE(operation_id,resolved_reception_id),
        CHECK (resolved_reception_id IS NULL OR resolved_reception_id=requested_reception_id),
        CHECK (outcome='rejected' OR (resolved_reception_id IS NOT NULL AND resulting_revision IS NOT NULL)),
        CHECK ((outcome='rejected' AND response_status>=400)
            OR (outcome<>'rejected' AND response_status<300))
    )""",
    f"""CREATE TABLE data_quality_issues (
        issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reception_id TEXT NOT NULL REFERENCES receptions(id),
        correction_event_id INTEGER NOT NULL REFERENCES reception_events(event_id),
        issue_kind TEXT NOT NULL CHECK (issue_kind IN
            ('invalidated_seating','snapshot_context','prediction_gate','workflow_impact')),
        evidence_event_id INTEGER,
        policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) BETWEEN 1 AND 64),
        {document('evidence_json')}, {timestamp('recorded_at')},
        UNIQUE(correction_event_id,reception_id,issue_kind),
        UNIQUE(issue_id,reception_id),
        FOREIGN KEY(evidence_event_id,reception_id) REFERENCES reception_events(event_id,reception_id)
    )""",
    f"""CREATE TABLE data_quality_reviews (
        review_id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER NOT NULL,
        reception_id TEXT NOT NULL,
        {integer('review_number', minimum=1)},
        operation_id TEXT NOT NULL UNIQUE,
        decision TEXT NOT NULL CHECK (decision IN ('hold','exclude','clear')),
        reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) BETWEEN 1 AND 64),
        reason_detail TEXT NOT NULL CHECK (length(trim(reason_detail)) BETWEEN 1 AND 500),
        {timestamp('recorded_at')},
        UNIQUE(issue_id,review_number),
        FOREIGN KEY(issue_id,reception_id) REFERENCES data_quality_issues(issue_id,reception_id),
        FOREIGN KEY(operation_id,reception_id) REFERENCES staff_operations(operation_id,resolved_reception_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    f"""CREATE TABLE data_quality_assessments (
        assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reception_id TEXT NOT NULL REFERENCES receptions(id),
        {integer('reception_revision')},
        valid_seated_event_id INTEGER,
        operation_id TEXT NOT NULL UNIQUE,
        decision TEXT NOT NULL CHECK (decision IN ('hold','exclude','eligible')),
        policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) BETWEEN 1 AND 64),
        {integer('checked_through_event_id')}, {integer('issues_through_id')},
        {integer('reviews_through_id')},
        reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) BETWEEN 1 AND 64),
        reason_detail TEXT NOT NULL CHECK (length(trim(reason_detail)) BETWEEN 1 AND 500),
        {timestamp('recorded_at')},
        CHECK (decision<>'eligible' OR valid_seated_event_id IS NOT NULL),
        FOREIGN KEY(valid_seated_event_id,reception_id) REFERENCES reception_events(event_id,reception_id),
        FOREIGN KEY(operation_id,reception_id) REFERENCES staff_operations(operation_id,resolved_reception_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
    f"""CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version=2),
        {uuid_column('migration_id')} UNIQUE,
        from_version INTEGER NOT NULL CHECK (from_version=1),
        {timestamp('applied_at')},
        code_sha256 TEXT NOT NULL CHECK (length(code_sha256)=64 AND code_sha256 NOT GLOB '*[^0-9a-f]*'),
        source_manifest_sha256 TEXT NOT NULL CHECK (length(source_manifest_sha256)=64
            AND source_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
        {integer('legacy_max_event_id')}, {integer('legacy_event_sequence')},
        {integer('legacy_reception_sequence')}
    )""",
    'CREATE INDEX events_reception ON reception_events(reception_id,event_id)',
    'CREATE UNIQUE INDEX events_operation ON reception_events(operation_id) WHERE operation_id IS NOT NULL',
    'CREATE UNIQUE INDEX events_correction ON reception_events(corrects_event_id) WHERE corrects_event_id IS NOT NULL',
    'CREATE UNIQUE INDEX events_revision ON reception_events(reception_id,revision_after) WHERE revision_after IS NOT NULL',
    'CREATE INDEX operations_reception ON staff_operations(resolved_reception_id,recorded_at)',
    'CREATE INDEX issues_reception ON data_quality_issues(reception_id,issue_id)',
    'CREATE INDEX reviews_issue ON data_quality_reviews(issue_id,review_id)',
    'CREATE INDEX assessments_reception ON data_quality_assessments(reception_id,assessment_id)',
)

TRIGGERS = [
    """CREATE TRIGGER metadata_no_delete BEFORE DELETE ON store_metadata
       BEGIN SELECT RAISE(ABORT,'store identity is retained'); END""",
    """CREATE TRIGGER metadata_identity_immutable BEFORE UPDATE ON store_metadata
       WHEN NEW.database_id IS NOT OLD.database_id OR NEW.singleton IS NOT OLD.singleton
       BEGIN SELECT RAISE(ABORT,'store identity is immutable'); END""",
    """CREATE TRIGGER metadata_no_staff_downgrade BEFORE UPDATE ON store_metadata
       WHEN OLD.staff_write_mode='revision_required' AND NEW.staff_write_mode='legacy'
       BEGIN SELECT RAISE(ABORT,'legacy staff writes cannot be re-enabled'); END""",
    """CREATE TRIGGER events_no_legacy_insert BEFORE INSERT ON reception_events
       WHEN NEW.audit_kind='legacy_v1' BEGIN SELECT RAISE(ABORT,'legacy history is frozen'); END""",
    """CREATE TRIGGER events_corrections_disabled BEFORE INSERT ON reception_events
       WHEN NEW.event_type='seating_corrected'
        AND (SELECT seat_corrections_enabled FROM store_metadata WHERE singleton=1)=0
       BEGIN SELECT RAISE(ABORT,'seat corrections are disabled'); END""",
    """CREATE TRIGGER events_legacy_mode BEFORE INSERT ON reception_events
       WHEN NEW.audit_kind='staff_legacy_v2'
        AND (SELECT staff_write_mode FROM store_metadata WHERE singleton=1)<>'legacy'
       BEGIN SELECT RAISE(ABORT,'versioned staff operation required'); END""",
    """CREATE TRIGGER events_correction_target BEFORE INSERT ON reception_events
       WHEN NEW.event_type='seating_corrected' AND NOT EXISTS (
         SELECT 1 FROM reception_events e WHERE e.event_id=NEW.corrects_event_id
          AND e.reception_id=NEW.reception_id AND e.event_type='seated'
          AND e.from_status=NEW.to_status
          AND e.event_id=(SELECT MAX(event_id) FROM reception_events WHERE reception_id=NEW.reception_id))
       BEGIN SELECT RAISE(ABORT,'invalid correction target'); END""",
    """CREATE TRIGGER issues_correction_target BEFORE INSERT ON data_quality_issues
       WHEN NOT EXISTS (SELECT 1 FROM reception_events WHERE event_id=NEW.correction_event_id
         AND event_type='seating_corrected')
       BEGIN SELECT RAISE(ABORT,'invalid quality cause'); END""",
    """CREATE TRIGGER reviews_sequence BEFORE INSERT ON data_quality_reviews
       WHEN NEW.review_number<>COALESCE((SELECT MAX(review_number)+1 FROM data_quality_reviews
         WHERE issue_id=NEW.issue_id),1)
       BEGIN SELECT RAISE(ABORT,'stale quality review'); END""",
    """CREATE TRIGGER assessments_valid_seating BEFORE INSERT ON data_quality_assessments
       WHEN NEW.valid_seated_event_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM reception_events e WHERE e.event_id=NEW.valid_seated_event_id
          AND e.reception_id=NEW.reception_id AND e.event_type='seated'
          AND NOT EXISTS (SELECT 1 FROM reception_events c WHERE c.corrects_event_id=e.event_id))
       BEGIN SELECT RAISE(ABORT,'assessment references an invalid seating'); END""",
    """CREATE TRIGGER assessments_eligibility BEFORE INSERT ON data_quality_assessments
       WHEN NEW.decision='eligible' AND (
         NOT EXISTS (SELECT 1 FROM receptions r WHERE r.id=NEW.reception_id
          AND r.status='seated' AND r.revision=NEW.reception_revision)
         OR NEW.checked_through_event_id<>(SELECT COALESCE(MAX(event_id),0) FROM reception_events)
         OR NEW.issues_through_id<>(SELECT COALESCE(MAX(issue_id),0) FROM data_quality_issues)
         OR NEW.reviews_through_id<>(SELECT COALESCE(MAX(review_id),0) FROM data_quality_reviews)
         OR EXISTS (SELECT 1 FROM data_quality_issues i WHERE i.reception_id=NEW.reception_id
          AND COALESCE((SELECT decision FROM data_quality_reviews v WHERE v.issue_id=i.issue_id
            ORDER BY v.review_id DESC LIMIT 1),'hold')<>'clear'))
       BEGIN SELECT RAISE(ABORT,'unreviewed or stale training eligibility'); END""",
    """CREATE TRIGGER receptions_revision BEFORE UPDATE ON receptions
       WHEN NEW.revision<>OLD.revision+1
       BEGIN SELECT RAISE(ABORT,'revision must advance exactly once'); END""",
    """CREATE TRIGGER receptions_intake_immutable BEFORE UPDATE ON receptions
       WHEN NEW.reception_order IS NOT OLD.reception_order OR NEW.id IS NOT OLD.id
         OR NEW.display_number IS NOT OLD.display_number OR NEW.received_at IS NOT OLD.received_at
         OR NEW.adults IS NOT OLD.adults OR NEW.children IS NOT OLD.children
         OR NEW.total_people IS NOT OLD.total_people OR NEW.recorded_at IS NOT OLD.recorded_at
         OR NEW.source IS NOT OLD.source
       BEGIN SELECT RAISE(ABORT,'intake is immutable'); END""",
    """CREATE TRIGGER receptions_no_delete BEFORE DELETE ON receptions
       BEGIN SELECT RAISE(ABORT,'reception history is retained'); END""",
]
for _table in ('reception_events','reception_snapshots','predictions','idempotency_keys',
               'staff_operations','data_quality_issues','data_quality_reviews',
               'data_quality_assessments','schema_migrations'):
    for _action in ('UPDATE','DELETE'):
        TRIGGERS.append(f"""CREATE TRIGGER {_table}_no_{_action.lower()} BEFORE {_action} ON {_table}
            BEGIN SELECT RAISE(ABORT,'audit records are immutable'); END""")
# REPLACE can delete a conflicting row without firing DELETE triggers unless
# recursive_triggers is enabled. Reject conflicting INSERTs before that happens.
_INSERT_KEYS = {
    'store_metadata': [('singleton',)],
    'receptions': [('reception_order',),('id',),('display_number',)],
    'reception_snapshots': [('reception_id',)], 'predictions': [('reception_id',)],
    'idempotency_keys': [('key',),('reception_id',)],
    'reception_events': [('event_id',),('operation_id',),('corrects_event_id',),('reception_id','revision_after')],
    'staff_operations': [('operation_id',)],
    'data_quality_issues': [('issue_id',),('correction_event_id','reception_id','issue_kind')],
    'data_quality_reviews': [('review_id',),('operation_id',),('issue_id','review_number')],
    'data_quality_assessments': [('assessment_id',),('operation_id',)],
    'schema_migrations': [('version',),('migration_id',)],
}
for _table,_keys in _INSERT_KEYS.items():
    _condition=' OR '.join('('+' AND '.join(f'{column}=NEW.{column}' for column in key)+')' for key in _keys)
    TRIGGERS.append(f"""CREATE TRIGGER {_table}_no_replace BEFORE INSERT ON {_table}
        WHEN EXISTS (SELECT 1 FROM {_table} WHERE {_condition})
        BEGIN SELECT RAISE(ABORT,'existing immutable identity cannot be replaced'); END""")
TRIGGERS = tuple(TRIGGERS)


def signature(conn):
    return tuple((kind, name, table, re.sub(r'\s+', ' ', sql).strip() if sql else None)
                 for kind, name, table, sql in conn.execute(
                     "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"))


def rebuild_tables(conn, database_id, write_epoch, checkpoint=lambda stage: None):
    conn.execute(REVISION_SQL)
    checkpoint('revision_added')
    conn.execute(METADATA_SQL)
    conn.execute('INSERT INTO new_store_metadata(singleton,last_recorded_at,database_id,write_epoch) '
                 'SELECT singleton,last_recorded_at,?,? FROM store_metadata', (database_id,write_epoch))
    conn.execute('DROP TABLE store_metadata')
    conn.execute('ALTER TABLE new_store_metadata RENAME TO store_metadata')
    checkpoint('metadata_rebuilt')
    conn.execute(EVENTS_SQL)
    conn.execute("INSERT INTO new_reception_events(event_id,reception_id,event_type,from_status,to_status,"
                 "occurred_at,recorded_at,source,audit_kind) SELECT event_id,reception_id,event_type,"
                 "from_status,to_status,occurred_at,recorded_at,source,'legacy_v1' FROM reception_events "
                 "ORDER BY event_id")
    checkpoint('events_copied')
    conn.execute('DROP TABLE reception_events')
    conn.execute('ALTER TABLE new_reception_events RENAME TO reception_events')
    checkpoint('events_replaced')
    for sql in AUXILIARY_SQL:
        conn.execute(sql)
    checkpoint('auxiliary_created')


@lru_cache(maxsize=2)
def expected_signature(version):
    if version not in (1,2):
        raise ValueError('Unsupported schema version.')
    conn = sqlite3.connect(':memory:', isolation_level=None)
    try:
        for sql in V1_SCHEMA:
            conn.execute(sql)
        if version == 2:
            rebuild_tables(conn, '0'*36, '0'*36)
            for sql in TRIGGERS:
                conn.execute(sql)
        return signature(conn)
    finally:
        conn.close()
