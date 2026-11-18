"""Explicit synthetic-only v1 -> v2 migration and exact legacy-data comparison."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from reception_schema import (
    SchemaValidationError, integrity as check_integrity,
    validate_history, validate_schema as validate_database_schema,
)

from .safety import SafetyError, Workspace
from .schema import TRIGGERS, rebuild_tables


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
    result['sqlite_sequence'] = tuple(tuple(row) for row in conn.execute(
        "SELECT name,seq,typeof(seq) FROM sqlite_sequence WHERE name IN ('receptions','reception_events') ORDER BY name"))
    return result


def integrity(conn):
    try:
        return check_integrity(conn)
    except SchemaValidationError as exc:
        raise MigrationError(str(exc)) from exc


def validate_schema(conn, version):
    try:
        return validate_database_schema(conn, version)
    except SchemaValidationError as exc:
        raise MigrationError(str(exc)) from exc


def validate_v1_history(conn):
    try:
        return validate_history(conn, 1)
    except SchemaValidationError as exc:
        raise MigrationError(str(exc)) from exc


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
            code += Path(__file__).parents[1].joinpath('reception_schema.py').read_bytes()
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
