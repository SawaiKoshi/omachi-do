"""Explicit synthetic-only v1 -> v2 migration and exact legacy-data comparison."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
from uuid import uuid4

from .safety import SafetyError, Workspace
from .schema import TRIGGERS, expected_signature, rebuild_tables, signature


class MigrationError(ValueError):
    """Schema, historical data, or preservation checks failed."""


class PostCommitValidationError(MigrationError):
    """COMMIT succeeded; this output must be quarantined, not called rolled back."""


STAGES = ('preflight_complete','transaction_started','revision_added','metadata_rebuilt',
          'events_copied','events_replaced','auxiliary_created','sequences_restored',
          'triggers_created','preservation_verified','version_set','before_commit')
LEGACY_TABLES = ('store_metadata','daily_counters','receptions','reception_snapshots',
                 'predictions','reception_events','idempotency_keys')


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',',':'))


def digest(value):
    return sha256(json_text(value).encode()).hexdigest()


def _quoted(name):
    return '"' + name.replace('"','""') + '"'


def _cell(value, storage_type):
    if storage_type == 'real':
        value = value.hex()
    elif storage_type == 'blob':
        value = value.hex()
    return (storage_type,value)


def legacy_manifest(conn, columns=None):
    """Compare values AND SQLite storage classes; never parse/re-encode saved JSON."""
    if columns is None:
        columns = {table: tuple(row[1] for row in conn.execute(f'PRAGMA table_info({_quoted(table)})'))
                   for table in LEGACY_TABLES}
    result = {}
    for table, names in columns.items():
        selection = ','.join(item for name in names
                             for item in (_quoted(name), f'typeof({_quoted(name)})'))
        rows = [tuple(_cell(row[i],row[i+1]) for i in range(0,len(row),2))
                for row in conn.execute(f'SELECT {selection} FROM {_quoted(table)}')]
        result[table] = {'columns': names, 'rows': sorted(rows,key=json_text)}
    result['sqlite_sequence'] = tuple(conn.execute(
        "SELECT name,seq,typeof(seq) FROM sqlite_sequence WHERE name IN ('receptions','reception_events') ORDER BY name"))
    return result


def integrity(conn):
    if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise MigrationError('SQLite integrity_check failed.')
    if conn.execute('PRAGMA foreign_key_check').fetchall():
        raise MigrationError('Foreign-key violations; migration does not repair data.')


def validate_schema(conn, version):
    if conn.execute('PRAGMA user_version').fetchone()[0] != version:
        raise MigrationError(f'Expected schema v{version}; no automatic migration.')
    if signature(conn) != expected_signature(version):
        raise MigrationError('Incomplete or unexpected schema, constraints, indexes, or triggers.')
    integrity(conn)
    if version == 2:
        rows = conn.execute('SELECT singleton,database_id,write_epoch,staff_write_mode,'
                            'seat_corrections_enabled FROM store_metadata').fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise MigrationError('Invalid store metadata.')
        from uuid import UUID
        try:
            for value in rows[0][1:3]:
                if str(UUID(value)) != value or UUID(value).int == 0:
                    raise ValueError
        except (ValueError,TypeError,AttributeError) as exc:
            raise MigrationError('Invalid database identity or write epoch.') from exc
        if rows[0][3:] != ('legacy',0):
            raise MigrationError('This laboratory only accepts disabled corrections in legacy mode.')
        if conn.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=2 AND from_version=1').fetchone()[0] != 1:
            raise MigrationError('Missing v1 -> v2 migration provenance.')


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (TypeError,ValueError) as exc:
        raise MigrationError('Missing or invalid timezone-aware historical timestamp.') from exc


def validate_v1_history(conn):
    rows = conn.execute('SELECT singleton,last_recorded_at FROM store_metadata').fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise MigrationError('Missing singleton metadata.')
    last = _time(rows[0][1]) if rows[0][1] is not None else None
    allowed = {'waiting': {'called','seated','cancelled'},
               'called': {'absent','seated','cancelled'},
               'absent': {'waiting','called','seated','cancelled'},
               'seated': set(), 'cancelled': set()}
    states = {}
    for event_id, rid, kind, before, after, occurred, recorded in conn.execute(
            'SELECT event_id,reception_id,event_type,from_status,to_status,occurred_at,recorded_at '
            'FROM reception_events ORDER BY event_id'):
        if kind == 'registered':
            if rid in states or before is not None or after != 'waiting':
                raise MigrationError('Invalid registration event chain.')
        elif (rid not in states or states[rid] != before or kind != after
              or after not in allowed.get(before,set())):
            raise MigrationError('Invalid historical state transition.')
        if _time(recorded) < _time(occurred) or last is None or _time(recorded) > last:
            raise MigrationError('Historical event clock is inconsistent.')
        states[rid] = after
    for rid, status, received, seated, actual, recorded in conn.execute(
            'SELECT id,status,received_at,seated_at,actual_wait_minutes,recorded_at FROM receptions'):
        if states.get(rid) != status:
            raise MigrationError('Current status does not match the last event.')
        if last is None or _time(received) > last or _time(recorded) < _time(received):
            raise MigrationError('Reception clock is inconsistent.')
        if status == 'seated':
            expected = (_time(seated)-_time(received)).total_seconds()/60
            if not math.isfinite(actual) or not math.isclose(actual,expected,rel_tol=1e-12,abs_tol=1e-9):
                raise MigrationError('Actual wait does not match the original timestamps.')
        for table in ('reception_snapshots','predictions','idempotency_keys'):
            if conn.execute(f'SELECT COUNT(*) FROM {table} WHERE reception_id=?',(rid,)).fetchone()[0] != 1:
                raise MigrationError('Missing one-to-one intake data.')
    for table in ('reception_snapshots','predictions','idempotency_keys'):
        if conn.execute(f'SELECT COUNT(*) FROM {table} WHERE reception_id IS NULL').fetchone()[0]:
            raise MigrationError('NULL legacy primary/foreign key.')
    if conn.execute('SELECT COUNT(*) FROM idempotency_keys WHERE key IS NULL').fetchone()[0]:
        raise MigrationError('NULL legacy idempotency key.')
    for table, column in (('receptions','reception_order'),('reception_events','event_id')):
        maximum = conn.execute(f'SELECT COALESCE(MAX({column}),0) FROM {table}').fetchone()[0]
        sequence = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(table,)).fetchone()
        if maximum and (sequence is None or type(sequence[0]) is not int or sequence[0]<maximum):
            raise MigrationError('Invalid historical AUTOINCREMENT high-water mark.')
    for number, received in conn.execute('SELECT display_number,received_at FROM receptions'):
        day, suffix = number.rsplit('-',1)
        counter = conn.execute('SELECT last_number FROM daily_counters WHERE reception_date=?',(day,)).fetchone()
        if not suffix.isdecimal() or counter is None or counter[0]<int(suffix):
            raise MigrationError('Daily reception counter is inconsistent.')


def inspect_database(workspace, path, *, version, recovery=False):
    with workspace.connection(path,recovery=recovery) as conn:
        conn.execute('BEGIN')
        validate_schema(conn,version)
        result = legacy_manifest(conn) if version == 1 else {'schema_version':2}
        conn.rollback()
        return result


def migrate(workspace: Workspace, path, *, expected_sha256, checkpoint=lambda stage: None):
    """Migrate only a registered synthetic temp file. Hook is for fault injection.

    There are no application imports, predictor calls, automatic migrations, or
    commit-capable executescript calls. Every checkpoint precedes the sole COMMIT.
    """
    path = workspace.path(path)
    with workspace.connection(path) as conn:
        if sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise SafetyError('Source bytes changed; expected SHA-256 is mandatory.')
        if workspace.manifest['databases'].get(path.name) != expected_sha256:
            raise SafetyError('Source is not the registered synthetic fixture snapshot.')
        validate_schema(conn,1)
        validate_v1_history(conn)
        checkpoint('preflight_complete')
        if conn.in_transaction:
            raise MigrationError('Unexpected implicit transaction before FK toggle.')
        conn.execute('PRAGMA foreign_keys=OFF')
        if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 0:
            raise MigrationError('Foreign keys must be disabled before BEGIN.')
        try:
            conn.execute('BEGIN IMMEDIATE')
            checkpoint('transaction_started')
            if sha256(path.read_bytes()).hexdigest() != expected_sha256:
                raise SafetyError('Source changed before the exclusive writer lock was obtained.')
            validate_schema(conn,1)
            validate_v1_history(conn)
            before = legacy_manifest(conn)
            columns = {name: data['columns'] for name,data in before.items() if name != 'sqlite_sequence'}
            source_digest = digest(before)
            sequences = {name:seq for name,seq,kind in before['sqlite_sequence']}
            max_event = conn.execute('SELECT COALESCE(MAX(event_id),0) FROM reception_events').fetchone()[0]
            rebuild_tables(conn,str(uuid4()),str(uuid4()),checkpoint)
            for table in ('receptions','reception_events'):
                conn.execute('DELETE FROM sqlite_sequence WHERE name=?',(table,))
                if table in sequences:
                    conn.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)',(table,sequences[table]))
            checkpoint('sequences_restored')
            for sql in TRIGGERS:
                conn.execute(sql)
            checkpoint('triggers_created')
            if legacy_manifest(conn,columns) != before:
                raise MigrationError('Legacy columns, storage classes, or sequences changed.')
            if conn.execute('SELECT COUNT(*) FROM receptions WHERE revision<>0').fetchone()[0]:
                raise MigrationError('Existing revisions must begin at zero.')
            integrity(conn)
            checkpoint('preservation_verified')
            code = b''.join(Path(__file__).with_name(name).read_bytes()
                            for name in ('migration.py','schema.py','v1_schema.py','safety.py'))
            conn.execute('INSERT INTO schema_migrations VALUES (2,?,1,?,?,?,?,?,?)',
                         (str(uuid4()),datetime.now(timezone.utc).isoformat(),sha256(code).hexdigest(),
                          source_digest,max_event,sequences.get('reception_events',0),sequences.get('receptions',0)))
            conn.execute('PRAGMA user_version=2')
            checkpoint('version_set')
            validate_schema(conn,2)
            checkpoint('before_commit')
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.execute('PRAGMA foreign_keys=ON')
            if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise MigrationError('Foreign keys were not restored after migration.')
    # A post-COMMIT failure is never reported as a successful rollback.
    try:
        with workspace.connection(path) as reopened:
            reopened.execute('BEGIN')
            validate_schema(reopened,2)
            if legacy_manifest(reopened,columns) != before:
                raise MigrationError('Post-COMMIT legacy data mismatch.')
            reopened.rollback()
    except Exception as exc:
        raise PostCommitValidationError(
            'COMMIT already succeeded. Quarantine this temporary output; no rollback was performed.'
        ) from exc
    return {'schema_version':2,'legacy_manifest_sha256':source_digest,
            'preserved_tables':list(LEGACY_TABLES),'revision':0,
            'staff_write_mode':'legacy','seat_corrections_enabled':0}


def copy_database(workspace, source, destination, *, recovery=False):
    """Backup or restore a synthetic DB into a NEW disposable temp file only."""
    with workspace.connection(source,readonly=not recovery,recovery=recovery) as src:
        integrity(src)
        target = workspace.new_file(destination)
        workspace.authorize_synthetic(target)
        with workspace.connection(target) as dst:
            src.backup(dst)
            integrity(dst)
    workspace.authorize_synthetic(target)
    return target
