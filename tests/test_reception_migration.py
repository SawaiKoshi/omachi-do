"""Migration laboratory: synthetic fixtures and backups live only in TemporaryDirectory."""

from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from reception_migration.migration import (
    LEGACY_TABLES, STAGES, MigrationError, copy_database, inspect_database,
    legacy_manifest, migrate, validate_schema,
)
from reception_migration.safety import (
    PROTECTED_FILES, SafetyError, Workspace, temporary_workspace,
)
from reception_migration.schema import TRIGGERS, signature
from reception_migration.synthetic import BASE, create_v1
from reception_migration.v1_schema import V1_SCHEMA
from reception_store import ReceptionStore, StoreValidationError, _SCHEMA


@pytest.fixture
def lab():
    with temporary_workspace() as workspace:
        yield workspace


def source_hash(path):
    return sha256(path.read_bytes()).hexdigest()


def run_migration(lab,path,**kwargs):
    return migrate(lab,path,expected_sha256=source_hash(path),**kwargs)


def snapshot(lab,path,**kwargs):
    with lab.connection(path,**kwargs) as conn:
        return (conn.execute('PRAGMA user_version').fetchone()[0],signature(conn),legacy_manifest(conn))


def old_columns(manifest):
    return {name:part['columns'] for name,part in manifest.items() if name!='sqlite_sequence'}


def test_frozen_v1_schema_is_identical_to_existing_application():
    assert V1_SCHEMA == _SCHEMA


@pytest.mark.parametrize('populated',[False,True])
def test_migration_preserves_every_legacy_value_type_identifier_json_and_sequence(lab,populated):
    path,ids = create_v1(lab,populated=populated)
    original = snapshot(lab,path)[2]
    result = run_migration(lab,path)
    assert result['preserved_tables'] == list(LEGACY_TABLES)
    assert result['revision'] == 0
    assert result['staff_write_mode'] == 'legacy'
    assert result['seat_corrections_enabled'] == 0
    with lab.connection(path) as conn:
        validate_schema(conn,2)
        assert legacy_manifest(conn,old_columns(original)) == original
        assert conn.execute('SELECT COUNT(*) FROM receptions WHERE revision<>0').fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM reception_events WHERE audit_kind<>'legacy_v1' "
                            'OR operation_id IS NOT NULL OR revision_after IS NOT NULL').fetchone()[0] == 0
        for table in ('staff_operations','data_quality_issues','data_quality_reviews','data_quality_assessments'):
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert conn.execute('SELECT staff_write_mode,seat_corrections_enabled FROM store_metadata').fetchone() == ('legacy',0)
        if populated:
            assert {r[0] for r in conn.execute('SELECT DISTINCT status FROM receptions')} == {
                'waiting','called','absent','seated','cancelled'}
            assert conn.execute('SELECT actual_wait_minutes FROM receptions WHERE id=?',(ids['zero'],)).fetchone()[0] == 0.0
            assert conn.execute('SELECT actual_wait_minutes FROM receptions WHERE id=?',(ids['fraction'],)).fetchone()[0] == pytest.approx(0.5020576)
            assert conn.execute('SELECT actual_wait_minutes FROM receptions WHERE id=?',(ids['overnight'],)).fetchone()[0] == 2.0
            assert conn.execute('SELECT COUNT(*) FROM reception_events WHERE occurred_at=?',(BASE.isoformat(),)).fetchone()[0]>5


@pytest.mark.parametrize('stage',STAGES)
@pytest.mark.parametrize('populated',[False,True])
def test_each_checkpoint_exception_completely_rolls_back(lab,stage,populated):
    path,_ = create_v1(lab,populated=populated)
    original = snapshot(lab,path)
    original_bytes = path.read_bytes()
    seen=[]

    def failure(point):
        seen.append(point)
        if point==stage:
            raise RuntimeError('injected migration failure')

    with pytest.raises(RuntimeError,match='injected'):
        run_migration(lab,path,checkpoint=failure)
    assert seen[-1] == stage
    assert snapshot(lab,path) == original
    assert path.read_bytes() == original_bytes
    assert not Path(str(path)+'-journal').exists()
    assert run_migration(lab,path)['schema_version']==2


@pytest.mark.parametrize('stage',STAGES[1:])
def test_sigkill_before_commit_recovers_original_schema_values_and_sequences(lab,stage):
    path,_ = create_v1(lab)
    original = snapshot(lab,path)
    original_bytes = path.read_bytes()
    child = r'''
import os, signal, sys
from contextlib import contextmanager
from reception_migration.safety import Workspace
from reception_migration.migration import migrate
original_connection=Workspace.connection
@contextmanager
def small_cache(self,*args,**kwargs):
    with original_connection(self,*args,**kwargs) as conn:
        conn.execute('PRAGMA cache_size=1')
        yield conn
Workspace.connection=small_cache
def crash(point):
    if point==sys.argv[4]:
        os.kill(os.getpid(),signal.SIGKILL)
migrate(Workspace(sys.argv[1]),sys.argv[2],expected_sha256=sys.argv[3],checkpoint=crash)
'''
    proc = subprocess.run([sys.executable,'-B','-c',child,str(lab.root),str(path),source_hash(path),stage],
                          capture_output=True,text=True,timeout=15)
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    if stage=='before_commit':
        assert path.read_bytes()!=original_bytes  # uncommitted pages really reached the main file
    # Recovery is explicitly allowed only for this generated temporary file.
    assert snapshot(lab,path,recovery=True) == original
    assert path.read_bytes() == original_bytes
    # A cold journal may remain after a read-only recovery. Let SQLite copy the
    # recovered snapshot; never manually unlink a potentially hot journal.
    recovered=copy_database(lab,path,'recovered.sqlite3',recovery=True)
    assert snapshot(lab,recovered)==original
    assert run_migration(lab,recovered)['schema_version']==2


def test_backup_restore_to_new_disposable_db_and_real_v1_registration_seating(lab):
    path,_ = create_v1(lab)
    original = snapshot(lab,path)
    backup = copy_database(lab,path,'backup.sqlite3')
    backup_hash = source_hash(backup)
    run_migration(lab,path)
    restored = copy_database(lab,backup,'restored.sqlite3')
    assert snapshot(lab,restored) == original
    assert source_hash(backup) == backup_hash
    store = ReceptionStore(restored,clock=lambda: BASE.replace(day=29,hour=1))
    old_order_seq = dict((row[0],row[1]) for row in original[2]['sqlite_sequence'])['receptions']
    old_event_seq = dict((row[0],row[1]) for row in original[2]['sqlite_sequence'])['reception_events']
    key=str(uuid4())
    receipt,created=store.register(2,0,key)
    assert created and receipt['reception_order']>old_order_seq
    store.transition(receipt['id'],'seated')
    assert store.get(receipt['id'])['actual_wait_minutes']==0.0
    assert store.register(2,0,key)==(receipt,False)
    assert store.get_events(receipt['id'])[0]['event_id']>old_event_seq
    assert source_hash(backup)==backup_hash


@pytest.mark.parametrize('fault',['foreign_key','missing_snapshot','wait','chain','counter','null_key','schema'])
def test_inconsistent_v1_is_rejected_without_changes(lab,fault):
    path,ids = create_v1(lab)
    with lab.connection(path) as conn:
        if fault=='foreign_key':
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute("INSERT INTO predictions VALUES ('missing','2026-10-29T00:00:00+09:00','synthetic','{}')")
        elif fault=='missing_snapshot':
            conn.execute('DELETE FROM reception_snapshots WHERE reception_id=?',(ids['waiting'],))
        elif fault=='wait':
            conn.execute('UPDATE receptions SET actual_wait_minutes=99 WHERE id=?',(ids['zero'],))
        elif fault=='chain':
            conn.execute("UPDATE reception_events SET from_status='seated' WHERE reception_id=? AND event_type='called'",(ids['called'],))
        elif fault=='counter':
            conn.execute('UPDATE daily_counters SET last_number=1')
        elif fault=='null_key':
            conn.execute('UPDATE idempotency_keys SET key=NULL WHERE reception_id=?',(ids['waiting'],))
        else:
            conn.execute('DROP INDEX events_reception')
    lab.authorize_synthetic(path)
    original = path.read_bytes()
    with pytest.raises(MigrationError):
        run_migration(lab,path)
    assert path.read_bytes()==original


def test_empty_uninitialized_file_is_not_silently_initialized(lab):
    path=lab.new_file('empty.sqlite3')
    lab.authorize_synthetic(path)
    with pytest.raises(MigrationError,match='schema v1'):
        run_migration(lab,path)
    assert path.read_bytes()==b''


def test_double_migration_and_code_version_mismatches_refuse_without_writes(lab):
    path,_ = create_v1(lab)
    original=path.read_bytes()
    with pytest.raises(MigrationError,match='schema v2'):
        inspect_database(lab,path,version=2)
    assert path.read_bytes()==original
    run_migration(lab,path)
    migrated=path.read_bytes()
    with pytest.raises((SafetyError,MigrationError)):
        run_migration(lab,path)
    assert path.read_bytes()==migrated
    lab.authorize_synthetic(path)
    with pytest.raises(MigrationError,match='schema v1'):
        run_migration(lab,path)
    assert path.read_bytes()==migrated
    with pytest.raises(StoreValidationError):
        ReceptionStore(path)
    assert path.read_bytes()==migrated


@pytest.mark.parametrize('damage',['version_only','index','trigger','table'])
def test_incomplete_v2_never_passes_schema_validation(lab,damage):
    path,_ = create_v1(lab)
    if damage=='version_only':
        with lab.connection(path) as conn:
            conn.execute('PRAGMA user_version=2')
    else:
        run_migration(lab,path)
        with lab.connection(path) as conn:
            if damage=='index':
                conn.execute('DROP INDEX events_correction')
            elif damage=='trigger':
                conn.execute('DROP TRIGGER predictions_no_update')
            else:
                conn.execute('DROP TABLE data_quality_assessments')
    original=path.read_bytes()
    with pytest.raises(MigrationError):
        inspect_database(lab,path,version=2)
    assert path.read_bytes()==original


def test_foreign_key_switches_outside_transaction_and_sqlite_noop_inside(lab):
    path,_ = create_v1(lab)
    with lab.connection(path) as conn:
        assert conn.isolation_level is None and not conn.in_transaction
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('PRAGMA foreign_keys=OFF')
        assert conn.in_transaction
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
        conn.rollback()
        conn.execute('PRAGMA foreign_keys=OFF')
        assert not conn.in_transaction
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==0
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('PRAGMA foreign_keys=ON')
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==0
        conn.rollback()
        conn.execute('PRAGMA foreign_keys=ON')
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1


def test_explicit_command_only_accepts_matching_generated_fixture(lab):
    path,_ = create_v1(lab)
    args=[sys.executable,'-B','-m','reception_migration','--workspace',str(lab.root),
          '--database',str(path),'--expected-sha256',source_hash(path)]
    result=subprocess.run(args,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['seat_corrections_enabled']==0
    repeated=subprocess.run(args,capture_output=True,text=True,timeout=15)
    assert repeated.returncode==2


@pytest.mark.parametrize('name',[str(p) for p in PROTECTED_FILES]+[
    str(Path.home()/'omachi-do-backups/anything.sqlite3'),'../escape.sqlite3',
    '/tmp/arbitrary.sqlite3'])
def test_protected_or_outside_path_is_rejected_before_sqlite_open(lab,name,monkeypatch):
    def forbidden(*args,**kwargs):
        pytest.fail('SQLite must not open a protected/outside path')
    monkeypatch.setattr(sqlite3,'connect',forbidden)
    with pytest.raises(SafetyError):
        migrate(lab,name,expected_sha256='0'*64)


@pytest.mark.parametrize('kind',['symlink','hardlink','directory_symlink','journal_symlink'])
def test_link_aliases_never_write_the_target(lab,kind):
    # Protected-target simulation: actual protected files are never linked or written.
    with temporary_workspace() as target_lab:
        target,_ = create_v1(target_lab)
        original=target.read_bytes()
        alias=lab.root/'alias.sqlite3'
        if kind=='symlink':
            alias.symlink_to(target)
        elif kind=='hardlink':
            os.link(target,alias)
        elif kind=='directory_symlink':
            (lab.root/'linked').symlink_to(target_lab.root,target_is_directory=True)
            alias=lab.root/'linked'/target.name
        else:
            alias,_ = create_v1(lab,'alias.sqlite3')
            Path(str(alias)+'-journal').symlink_to(target)
        with pytest.raises(SafetyError):
            if kind in ('symlink','hardlink'):
                lab.authorize_synthetic(alias)
            migrate(lab,alias,expected_sha256=source_hash(target))
        assert target.read_bytes()==original


def test_missing_path_wrong_hash_unregistered_file_and_restore_overwrite_refuse(lab):
    path,_ = create_v1(lab)
    with pytest.raises(SafetyError):
        migrate(lab,path,expected_sha256='0'*64)
    missing=lab.root/'missing.sqlite3'
    with pytest.raises(SafetyError):
        migrate(lab,missing,expected_sha256='0'*64)
    assert not missing.exists()
    unregistered=lab.new_file('unregistered.sqlite3')
    with pytest.raises(SafetyError):
        migrate(lab,unregistered,expected_sha256=source_hash(unregistered))
    with pytest.raises(FileExistsError):
        copy_database(lab,path,path.name)


def test_arbitrary_directory_without_private_generated_workspace_is_rejected(tmp_path):
    with pytest.raises(SafetyError):
        Workspace(tmp_path)


def test_no_executescript_or_application_imports_in_migration_implementation():
    import ast
    package=Path(__file__).parents[1]/'reception_migration'
    for path in package.glob('*.py'):
        tree=ast.parse(path.read_text())
        assert not any(isinstance(n,ast.Attribute) and n.attr=='executescript' for n in ast.walk(tree))
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                assert all(a.name not in ('reception_store','prediction_api','prediction_service','model_artifact') for a in node.names)


def insert_row(conn,table,values):
    return conn.execute(f'INSERT INTO {table}('+','.join(values)+') VALUES ('+
                        ','.join('?' for _ in values)+')',tuple(values.values())).lastrowid


def operation(conn,rid,**updates):
    epoch=conn.execute('SELECT write_epoch FROM store_metadata').fetchone()[0]
    request='{ "reason": "synthetic only" }'
    values=dict(operation_id=str(uuid4()),request_schema_version=1,requested_reception_id=rid,
                resolved_reception_id=rid,action='seat',write_epoch=epoch,expected_revision=0,
                principal_kind='shared_staff_token',principal_id='synthetic-shared-principal',
                request_json=request,request_sha256=sha256(request.encode()).hexdigest(),
                outcome='state_changed',response_status=200,response_json='{}',
                resulting_revision=1,recorded_at=BASE.isoformat())
    values.update(updates)
    insert_row(conn,'staff_operations',values)
    return values['operation_id']


@pytest.mark.parametrize('field,value',[
    ('operation_id',None),('operation_id','bad'),('expected_revision',-1),
    ('expected_revision',0.25),('expected_revision',None),('action','arbitrary'),
    ('request_json','broken'),('request_json','[]'),('request_sha256','z'*64),
    ('principal_id',' '),('principal_kind','self_reported_name'),
    ('recorded_at','bad time'),('resolved_reception_id',None),
    ('resolved_reception_id','missing'),('resulting_revision',None),
    ('response_status',500),('outcome','unknown'),
])
def test_staff_operation_required_types_checks_json_and_foreign_keys(lab,field,value):
    path,ids=create_v1(lab)
    run_migration(lab,path)
    with lab.connection(path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            operation(conn,ids['waiting'],**{field:value})
        assert conn.execute('SELECT COUNT(*) FROM staff_operations').fetchone()[0]==0


def test_operation_id_reuse_does_not_overwrite_original_payload_or_response(lab):
    path,ids=create_v1(lab)
    run_migration(lab,path)
    with lab.connection(path) as conn:
        op=operation(conn,ids['waiting'])
        original=conn.execute('SELECT * FROM staff_operations WHERE operation_id=?',(op,)).fetchone()
        for updates in ({},{'request_json':'{"different":true}'},{'requested_reception_id':ids['called'],
                        'resolved_reception_id':ids['called']}):
            with pytest.raises(sqlite3.IntegrityError):
                operation(conn,ids['waiting'],operation_id=op,**updates)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE staff_operations SET response_json='{}' WHERE operation_id=?",(op,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT OR REPLACE INTO staff_operations SELECT * FROM staff_operations WHERE operation_id=?',(op,))
        assert conn.execute('SELECT * FROM staff_operations WHERE operation_id=?',(op,)).fetchone()==original
        operation(conn,str(uuid4()),resolved_reception_id=None,resulting_revision=None,
                  outcome='rejected',response_status=404)


def historical_quality_fixture(lab):
    """Fabricate an already-recorded history, with corrections NEVER enabled.

    This fixture loads historical rows before installing guards, just as a schema
    fixture does. It is separate from migrate() and does not exercise a correction
    workflow or bypass any check during a preservation test.
    """
    path,ids=create_v1(lab)
    run_migration(lab,path)
    with lab.connection(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        for sql in TRIGGERS:
            conn.execute('DROP TRIGGER '+sql.split()[2])
        rid=ids['zero']
        target=conn.execute("SELECT event_id FROM reception_events WHERE reception_id=? AND event_type='seated'",(rid,)).fetchone()[0]
        op=operation(conn,rid,action='seating_correction')
        conn.execute("UPDATE receptions SET status='waiting',seated_at=NULL,actual_wait_minutes=NULL,revision=1 WHERE id=?",(rid,))
        cause=insert_row(conn,'reception_events',dict(reception_id=rid,event_type='seating_corrected',
            from_status='seated',to_status='waiting',occurred_at=BASE.isoformat(),recorded_at=BASE.isoformat(),
            source='synthetic_historical_fixture',audit_kind='staff_v2',operation_id=op,
            revision_before=0,revision_after=1,corrects_event_id=target,reason_code='accidental_seating',
            before_state_json='{"status":"seated","actual_wait_minutes":0}',
            after_state_json='{"status":"waiting","actual_wait_minutes":null}'))
        for sql in TRIGGERS:
            conn.execute(sql)
        conn.commit()
        validate_schema(conn,2)
        assert conn.execute('SELECT seat_corrections_enabled FROM store_metadata').fetchone()[0]==0
    return path,ids,cause


def issue(conn,rid,cause,**updates):
    values=dict(reception_id=rid,correction_event_id=cause,issue_kind='invalidated_seating',
                policy_version='synthetic-policy-v1',evidence_json='{}',recorded_at=BASE.isoformat())
    values.update(updates)
    return insert_row(conn,'data_quality_issues',values)


@pytest.mark.parametrize('fault',['duplicate','missing_cause','wrong_cause','wrong_evidence',
                                 'kind','json','policy','null_reception'])
def test_quality_issue_cause_deduplication_and_foreign_keys(lab,fault):
    path,ids,cause=historical_quality_fixture(lab)
    with lab.connection(path) as conn:
        issue(conn,ids['zero'],cause)
        changes={'duplicate':{},'missing_cause':{'correction_event_id':999999},
                 'wrong_cause':{'correction_event_id':1},
                 'wrong_evidence':{'evidence_event_id':conn.execute(
                     'SELECT MIN(event_id) FROM reception_events WHERE reception_id=?',(ids['called'],)).fetchone()[0]},
                 'kind':{'issue_kind':'unknown'},'json':{'evidence_json':'[]'},
                 'policy':{'policy_version':' '},'null_reception':{'reception_id':None}}[fault]
        if fault!='duplicate':
            changes.setdefault('issue_kind','workflow_impact')
        with pytest.raises(sqlite3.IntegrityError):
            issue(conn,ids['zero'],cause,**changes)
        assert conn.execute('SELECT COUNT(*) FROM data_quality_issues').fetchone()[0]==1


def test_quality_reviews_are_ordered_append_only_and_bound_to_the_same_reception(lab):
    path,ids,cause=historical_quality_fixture(lab)
    with lab.connection(path) as conn:
        rid=ids['zero']
        problem=issue(conn,rid,cause)
        def review(number=1,**updates):
            op=operation(conn,rid,action='quality_review',outcome='review_added')
            values=dict(issue_id=problem,reception_id=rid,review_number=number,operation_id=op,
                        decision='hold',reason_code='needs_review',reason_detail='synthetic evidence',
                        recorded_at=BASE.isoformat())
            values.update(updates)
            return insert_row(conn,'data_quality_reviews',values)
        review()
        for number,updates in [(1,{}),(3,{}),(2,{'decision':'unknown'}),(2,{'reason_detail':' '}),
                               (2,{'reception_id':ids['called']})]:
            with pytest.raises(sqlite3.IntegrityError):
                review(number,**updates)
        review(2,decision='exclude')
        for table in ('data_quality_issues','data_quality_reviews'):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f'DELETE FROM {table}')
        assert conn.execute('SELECT decision FROM data_quality_reviews ORDER BY review_id DESC LIMIT 1').fetchone()[0]=='exclude'


def test_assessment_requires_explicit_eligibility_evidence_and_valid_watermarks(lab):
    path,ids=create_v1(lab)
    run_migration(lab,path)
    with lab.connection(path) as conn:
        rid=ids['fraction']
        seated=conn.execute("SELECT event_id FROM reception_events WHERE reception_id=? AND event_type='seated'",(rid,)).fetchone()[0]
        def assess(**updates):
            op=operation(conn,rid,action='quality_assessment',outcome='assessment_added',resulting_revision=0)
            values=dict(reception_id=rid,reception_revision=0,valid_seated_event_id=seated,
                operation_id=op,decision='hold',policy_version='synthetic-policy-v1',
                checked_through_event_id=seated,issues_through_id=0,reviews_through_id=0,
                reason_code='unreviewed',reason_detail='synthetic unreviewed record',recorded_at=BASE.isoformat())
            values.update(updates)
            return insert_row(conn,'data_quality_assessments',values)
        assert conn.execute('SELECT COUNT(*) FROM data_quality_assessments').fetchone()[0]==0
        assess(valid_seated_event_id=None)
        for changes in ({'decision':'eligible','valid_seated_event_id':None},
                        {'issues_through_id':-1},{'reviews_through_id':0.5},
                        {'valid_seated_event_id':999999},{'decision':'unknown'}):
            with pytest.raises(sqlite3.IntegrityError):
                assess(**changes)
        assert conn.execute("SELECT COUNT(*) FROM data_quality_assessments WHERE decision='eligible'").fetchone()[0]==0


def test_event_revision_uniqueness_deferred_operation_fk_and_disabled_correction_guard(lab):
    path,ids=create_v1(lab)
    run_migration(lab,path)
    with lab.connection(path) as conn:
        rid=ids['waiting']
        op=str(uuid4())
        values=dict(reception_id=rid,event_type='called',from_status='waiting',to_status='called',
            occurred_at=BASE.isoformat(),recorded_at=BASE.isoformat(),source='synthetic-test',
            audit_kind='staff_v2',operation_id=op,revision_before=0,revision_after=1,
            before_state_json='{}',after_state_json='{}')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE receptions SET status='called',called_at=received_at,revision=1 WHERE id=?",(rid,))
        insert_row(conn,'reception_events',values)  # allowed until COMMIT: operation FK is deferred
        with pytest.raises(sqlite3.IntegrityError):
            conn.commit()
        conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE receptions SET status='called',called_at=received_at,revision=1 WHERE id=?",(rid,))
        insert_row(conn,'reception_events',values)
        operation(conn,rid,operation_id=op,action='call')
        conn.commit()
        for changes in ({'operation_id':str(uuid4())},{'revision_after':3},
                        {'audit_kind':'legacy_v1','operation_id':None,'revision_before':None,'revision_after':None,
                         'before_state_json':None,'after_state_json':None}):
            with pytest.raises(sqlite3.IntegrityError):
                insert_row(conn,'reception_events',{**values,**changes})
        seated=conn.execute("SELECT event_id FROM reception_events WHERE reception_id=? AND event_type='seated'",(ids['zero'],)).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError,match='disabled'):
            insert_row(conn,'reception_events',{**values,'reception_id':ids['zero'],
                'event_type':'seating_corrected','from_status':'seated','to_status':'waiting',
                'corrects_event_id':seated,'reason_code':'accidental_seating','operation_id':str(uuid4())})
        assert conn.execute('SELECT seat_corrections_enabled FROM store_metadata').fetchone()[0]==0


def test_migrated_schema_accepts_synthetic_registration_and_seating_without_reusing_ids(lab):
    path,_=create_v1(lab)
    original=snapshot(lab,path)[2]
    saved=dict((name,seq) for name,seq,kind in original['sqlite_sequence'])
    run_migration(lab,path)
    with lab.connection(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        rid,key=str(uuid4()),str(uuid4())
        now=BASE.replace(day=29,hour=1)
        stamp=now.isoformat()
        day=now.strftime('%Y%m%d')
        conn.execute('UPDATE daily_counters SET last_number=last_number+1 WHERE reception_date=?',(day,))
        number=conn.execute('SELECT last_number FROM daily_counters WHERE reception_date=?',(day,)).fetchone()[0]
        order=insert_row(conn,'receptions',dict(id=rid,display_number=f'{day}-{number:03d}',received_at=stamp,
            adults=2,children=0,total_people=2,status='waiting',recorded_at=stamp,source='synthetic-v2'))
        groups=[row[0] for row in conn.execute("SELECT total_people FROM receptions WHERE status IN ('waiting','called','absent') AND id<>?",(rid,))]
        bands=[sum((0 if p<=2 else 1 if p<=4 else 2)==band for p in groups) for band in range(3)]
        conn.execute('INSERT INTO reception_snapshots VALUES (?,?,?,?,?,?,?,?,?)',
                     (rid,stamp,'synthetic-v2',len(groups),len(groups),bands[0],*bands))
        conn.execute('INSERT INTO predictions VALUES (?,?,?,?)',(rid,stamp,'synthetic-v2','{}'))
        conn.execute('INSERT INTO idempotency_keys VALUES (?,?,?,?,?)',(key,rid,2,0,'{"status":"waiting"}'))
        event=insert_row(conn,'reception_events',dict(reception_id=rid,event_type='registered',to_status='waiting',
            occurred_at=stamp,recorded_at=stamp,source='synthetic-v2',audit_kind='registration_v2',
            revision_after=0,after_state_json='{}'))
        assert order>saved['receptions'] and event>saved['reception_events']
        seated=(now+timedelta(minutes=2,seconds=30,microseconds=123456)).isoformat()
        wait=(datetime.fromisoformat(seated)-now).total_seconds()/60
        conn.execute("UPDATE receptions SET status='seated',seated_at=?,actual_wait_minutes=?,revision=1 WHERE id=?",(seated,wait,rid))
        insert_row(conn,'reception_events',dict(reception_id=rid,event_type='seated',from_status='waiting',to_status='seated',
            occurred_at=seated,recorded_at=seated,source='synthetic-v2',audit_kind='staff_legacy_v2',
            revision_before=0,revision_after=1,before_state_json='{}',after_state_json='{}'))
        conn.execute('UPDATE store_metadata SET last_recorded_at=? WHERE singleton=1',(seated,))
        conn.commit()
        assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert conn.execute('SELECT revision,actual_wait_minutes FROM receptions WHERE id=?',(rid,)).fetchone()==(1,wait)
        assert number==2  # deleted reception's daily number was not reused


@pytest.mark.parametrize('failure',[False,True])
def test_migration_trace_has_one_explicit_commit_and_restores_fk_after_exception(lab,monkeypatch,failure):
    path,_=create_v1(lab)
    connect=sqlite3.connect
    calls=[]
    class TracedConnection:
        def __init__(self,conn):
            self.conn=conn
        def __getattr__(self,name):
            return getattr(self.conn,name)
        def execute(self,sql,*args):
            calls.append((sql,self.conn.in_transaction))
            return self.conn.execute(sql,*args)
        def commit(self):
            calls.append(('COMMIT()',self.conn.in_transaction))
            return self.conn.commit()
        def rollback(self):
            calls.append(('ROLLBACK()',self.conn.in_transaction))
            return self.conn.rollback()
    def traced_connect(*args,**kwargs):
        conn=connect(*args,**kwargs)
        return TracedConnection(conn) if args[0]!=':memory:' else conn
    monkeypatch.setattr(sqlite3,'connect',traced_connect)
    def checkpoint(stage):
        if failure and stage=='events_replaced':
            raise RuntimeError('injected')
    if failure:
        with pytest.raises(RuntimeError):
            run_migration(lab,path,checkpoint=checkpoint)
    else:
        run_migration(lab,path,checkpoint=checkpoint)
    assert ('PRAGMA foreign_keys=OFF',False) in calls
    assert all(not inside for sql,inside in calls if sql=='PRAGMA foreign_keys=ON')
    assert sum(sql=='COMMIT()' for sql,inside in calls)==(0 if failure else 1)
    assert all(inside for sql,inside in calls if sql.startswith(('CREATE ','ALTER ','DROP ','INSERT ','DELETE ')))


@pytest.mark.parametrize('table',['receptions','reception_events','reception_snapshots',
                                'predictions','idempotency_keys','store_metadata','schema_migrations'])
def test_insert_or_replace_cannot_bypass_immutable_history(lab,table):
    path,_=create_v1(lab)
    run_migration(lab,path)
    with lab.connection(path) as conn:
        before=conn.execute(f'SELECT * FROM {table}').fetchall()
        assert before
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}')
        assert conn.execute(f'SELECT * FROM {table}').fetchall()==before


@pytest.mark.parametrize('alias',['symlink','hardlink'])
def test_replacing_already_registered_path_with_link_is_rejected_before_connect(lab,monkeypatch,alias):
    path,_=create_v1(lab)
    with temporary_workspace() as protected_lab:
        protected,_=create_v1(protected_lab)
        expected=protected.read_bytes()
        path.unlink()  # remove only this generated disposable fixture
        if alias=='symlink':
            path.symlink_to(protected)
        else:
            os.link(protected,path)
        def forbidden(*args,**kwargs):
            pytest.fail('SQLite must not connect to a link alias')
        monkeypatch.setattr(sqlite3,'connect',forbidden)
        with pytest.raises(SafetyError):
            migrate(lab,path,expected_sha256=source_hash(protected))
        assert protected.read_bytes()==expected


def test_migration_rechecks_source_after_obtaining_writer_lock(lab):
    path,_=create_v1(lab)
    # A different SQLite writer ignores our advisory file lock and commits in the
    # small interval after preflight. The second digest must refuse that snapshot.
    def changed(stage):
        if stage=='preflight_complete':
            conn=sqlite3.connect(path,isolation_level=None)
            try:
                conn.execute('UPDATE daily_counters SET last_number=last_number+1')
            finally:
                conn.close()
    with pytest.raises(SafetyError,match='Source changed'):
        run_migration(lab,path,checkpoint=changed)
    with lab.connection(path) as conn:
        validate_schema(conn,1)


def test_quality_eligibility_rejects_unreviewed_problems_even_with_an_old_approval(lab):
    path,ids,cause=historical_quality_fixture(lab)
    with lab.connection(path) as conn:
        rid=ids['fraction']
        seat=conn.execute("SELECT event_id FROM reception_events WHERE reception_id=? AND event_type='seated'",(rid,)).fetchone()[0]
        values=dict(reception_id=rid,reception_revision=0,valid_seated_event_id=seat,
                    operation_id=operation(conn,rid,action='quality_assessment',outcome='assessment_added',resulting_revision=0),
                    decision='eligible',policy_version='synthetic-policy-v1',
                    checked_through_event_id=conn.execute('SELECT MAX(event_id) FROM reception_events').fetchone()[0],
                    issues_through_id=0,reviews_through_id=0,reason_code='verified',
                    reason_detail='synthetic assessed record',recorded_at=BASE.isoformat())
        insert_row(conn,'data_quality_assessments',values)
        issue_id=issue(conn,rid,cause,issue_kind='workflow_impact')
        values['operation_id']=operation(conn,rid,action='quality_assessment',outcome='assessment_added',resulting_revision=0)
        for watermark in (0,issue_id):
            with pytest.raises(sqlite3.IntegrityError,match='eligibility'):
                insert_row(conn,'data_quality_assessments',{**values,'issues_through_id':watermark})
        assert conn.execute('SELECT COUNT(*) FROM data_quality_assessments').fetchone()[0]==1


def test_post_commit_verification_error_is_not_reported_as_rollback(lab,monkeypatch):
    import reception_migration.migration as migration_module
    path,_=create_v1(lab)
    original=snapshot(lab,path)[2]
    validate=migration_module.validate_schema
    checked=0
    def fail_after_commit(conn,version):
        nonlocal checked
        if version==2:
            checked+=1
            if checked==2:
                raise MigrationError('injected post-commit read failure')
        return validate(conn,version)
    monkeypatch.setattr(migration_module,'validate_schema',fail_after_commit)
    with pytest.raises(migration_module.PostCommitValidationError,match='COMMIT already succeeded'):
        run_migration(lab,path)
    with lab.connection(path) as conn:
        validate(conn,2)
        assert legacy_manifest(conn,old_columns(original))==original


def test_sigkill_immediately_after_commit_leaves_complete_v2(lab):
    path,_=create_v1(lab)
    original=snapshot(lab,path)[2]
    child=r'''
from contextlib import contextmanager
import os,signal,sys
from reception_migration.safety import Workspace
from reception_migration.migration import migrate
original=Workspace.connection
class KillAfterCommit:
    def __init__(self,conn):
        self.conn=conn
    def __getattr__(self,name):
        return getattr(self.conn,name)
    def commit(self):
        self.conn.commit()
        os.kill(os.getpid(),signal.SIGKILL)
@contextmanager
def crash_connection(self,*args,**kwargs):
    with original(self,*args,**kwargs) as conn:
        yield KillAfterCommit(conn)
Workspace.connection=crash_connection
migrate(Workspace(sys.argv[1]),sys.argv[2],expected_sha256=sys.argv[3])
'''
    proc=subprocess.run([sys.executable,'-B','-c',child,str(lab.root),str(path),source_hash(path)],
                        capture_output=True,text=True,timeout=15)
    assert proc.returncode==-signal.SIGKILL,proc.stderr
    with lab.connection(path,recovery=True) as conn:
        validate_schema(conn,2)
        assert legacy_manifest(conn,old_columns(original))==original
