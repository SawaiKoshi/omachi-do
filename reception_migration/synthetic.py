"""Generate frozen-v1 synthetic fixtures exclusively inside temporary_workspace()."""

from datetime import datetime, timedelta
import json
import sqlite3
from uuid import uuid4

from .v1_schema import V1_SCHEMA


BASE = datetime.fromisoformat('2026-10-28T23:59:00+09:00')


def create_v1(workspace, name='synthetic.sqlite3', *, populated=True):
    path = workspace.new_file(name)
    conn = sqlite3.connect(path,isolation_level=None)
    ids = {}
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        for sql in V1_SCHEMA:
            conn.execute(sql)
        conn.execute('INSERT INTO store_metadata(singleton) VALUES(1)')
        conn.execute('PRAGMA user_version=1')

        def register(label, people, now):
            rid, key = str(uuid4()), str(uuid4())
            day = now.strftime('%Y%m%d')
            conn.execute('INSERT INTO daily_counters VALUES (?,1) ON CONFLICT(reception_date) '
                         'DO UPDATE SET last_number=last_number+1',(day,))
            number = conn.execute('SELECT last_number FROM daily_counters WHERE reception_date=?',(day,)).fetchone()[0]
            groups = conn.execute("SELECT total_people FROM receptions WHERE status IN ('waiting','called','absent')").fetchall()
            bands = [sum((0 if p[0]<=2 else 1 if p[0]<=4 else 2)==band for p in groups) for band in range(3)]
            same = bands[0 if people<=2 else 1 if people<=4 else 2]
            stamp = now.isoformat()
            conn.execute("INSERT INTO receptions(id,display_number,received_at,adults,children,total_people,"
                         "status,recorded_at,source) VALUES (?,?,?,?,?,?,'waiting',?,'synthetic_fixture_v1')",
                         (rid,f'{day}-{number:03d}',stamp,people,0,people,stamp))
            conn.execute('INSERT INTO reception_snapshots VALUES (?,?,?,?,?,?,?,?,?)',
                         (rid,stamp,'synthetic_fixture_v1',len(groups),len(groups),same,*bands))
            prediction = ' { "status": "available", "minutes": 12.25, "model_version": "synthetic-only", "warnings": ["合成\\u30c6スト"] } '
            conn.execute('INSERT INTO predictions VALUES (?,?,?,?)',(rid,stamp,'synthetic_fixture_v1',prediction))
            conn.execute("INSERT INTO reception_events(reception_id,event_type,from_status,to_status,occurred_at,recorded_at,source) "
                         "VALUES (?,'registered',NULL,'waiting',?,?,'synthetic_fixture_v1')",(rid,stamp,stamp))
            response = json.dumps({'id':rid,'display_number':f'{day}-{number:03d}','status':'waiting',
                                   'prediction':json.loads(prediction)},ensure_ascii=False,indent=2)+'\n'
            conn.execute('INSERT INTO idempotency_keys VALUES (?,?,?,?,?)',(key,rid,people,0,response))
            conn.execute('UPDATE store_metadata SET last_recorded_at=? WHERE singleton=1',(stamp,))
            ids[label] = rid
            return rid

        def transition(label, target, now):
            rid = ids[label]
            before,received,called = conn.execute('SELECT status,received_at,called_at FROM receptions WHERE id=?',(rid,)).fetchone()
            stamp = now.isoformat()
            wait = (now-datetime.fromisoformat(received)).total_seconds()/60 if target=='seated' else None
            conn.execute('UPDATE receptions SET status=?,called_at=?,seated_at=?,cancelled_at=?,actual_wait_minutes=? WHERE id=?',
                         (target,stamp if target=='called' else called,stamp if target=='seated' else None,
                          stamp if target=='cancelled' else None,wait,rid))
            conn.execute('INSERT INTO reception_events(reception_id,event_type,from_status,to_status,occurred_at,recorded_at,source) '
                         "VALUES (?,?,?,?,?,?,'synthetic_fixture_v1')",(rid,target,before,target,stamp,stamp))
            conn.execute('UPDATE store_metadata SET last_recorded_at=? WHERE singleton=1',(stamp,))

        def remove(label):
            rid = ids.pop(label)
            for table in ('idempotency_keys','predictions','reception_snapshots','reception_events','receptions'):
                conn.execute(f'DELETE FROM {table} WHERE '+('id' if table=='receptions' else 'reception_id')+'=?',(rid,))

        if populated:
            register('waiting',1,BASE)
            register('discard_middle',2,BASE)
            remove('discard_middle')
            for label,people in [('called',4),('absent',5),('zero',2),('fraction',3),('overnight',4),('cancelled',2)]:
                register(label,people,BASE)
            transition('called','called',BASE)
            transition('absent','called',BASE)
            transition('absent','absent',BASE)
            transition('zero','seated',BASE)
            transition('fraction','seated',BASE+timedelta(seconds=30,microseconds=123456))
            transition('overnight','seated',BASE+timedelta(minutes=2))
            transition('cancelled','cancelled',BASE+timedelta(minutes=2))
            register('discard_high_water',1,BASE+timedelta(minutes=2))
            remove('discard_high_water')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    workspace.authorize_synthetic(path)
    return path, ids
