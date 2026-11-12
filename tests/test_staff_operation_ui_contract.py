"""Synthetic API/JS serialization contracts; this does not enable browser UI."""

import json
from contextlib import closing
from pathlib import Path
import shutil
import subprocess
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from staff_call_absent_https_lab import synthetic_app


PROJECT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://staff.localhost:8445'
TOKEN = 'synthetic-operation-contract-token-not-for-real-use-1234567890'
NODE_VALIDATOR = """
import { readFileSync } from 'node:fs';
import { validOperationReceipt, validOperationContext, receiptMatchesCurrent }
  from './web/staff/assets/staff-operation-contract.mjs';
const { receipt, record, before, current, historical } = JSON.parse(readFileSync(0, 'utf8'));
if (!validOperationContext(before, record.reception_id)
    || !validOperationContext(current, record.reception_id)
    || !validOperationReceipt(receipt, record, before.reception)
    || !receiptMatchesCurrent(receipt, current.reception)) process.exit(1);
if (historical && (!validOperationReceipt(historical.receipt, historical.record)
    || !receiptMatchesCurrent(historical.receipt, current.reception))) process.exit(2);
// Manual v2 and legacy v1 markers must remain action-specific without a body.
const marker = { version: 2, state: 'manual', operation_id: record.operation_id,
  reception_id: record.reception_id, database_id: record.database_id,
  action: record.request.action, reason: 'expired' };
if (!validOperationReceipt(receipt, marker)) process.exit(3);
if (record.request.action === 'call') {
  delete marker.action; marker.version = 1;
  if (!validOperationReceipt(receipt, marker)) process.exit(4);
}
"""


@pytest.mark.parametrize('action,status,outcome', [
    ('call', 'waiting', 'state_changed'), ('call', 'absent', 'state_changed'),
    ('call', 'called', 'no_change'), ('absent', 'called', 'state_changed'),
    ('absent', 'absent', 'no_change'), ('absent', 'waiting', 'rejected'),
    ('absent', 'seated', 'rejected'), ('absent', 'cancelled', 'rejected'),
])
def test_synthetic_api_receipts_match_common_action_contracts(action, status, outcome):
    node = shutil.which('node')
    assert node is not None, 'Node.js is required for the API/JS serialization contract.'
    with synthetic_app(TOKEN, 8445) as wrapper:
        with TestClient(wrapper, base_url=ORIGIN, client=('127.0.0.1', 50000)) as client:
            login = client.post('/staff/session', headers={'Origin': ORIGIN, 'X-Staff-Token': TOKEN})
            assert login.status_code == 200
            headers = {'Origin': ORIGIN, 'X-CSRF-Token': login.json()['csrf_token']}
            # Cancelled rows intentionally do not appear in the staff lists.
            # Locate the fixture only in this newly generated synthetic DB;
            # the contract itself still uses authenticated HTTP context reads.
            database = wrapper.app.state.store.db_path
            assert database.parent.parent == Path('/tmp')
            assert database.parent.name.startswith('omachi-migration-')
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
                conn.execute('PRAGMA query_only=ON')
                reception_id = conn.execute('SELECT id FROM receptions WHERE status=? ORDER BY reception_order LIMIT 1',
                                            (status,)).fetchone()[0]
            row = client.get(f'/staff/receptions/{reception_id}/operation-context').json()['reception']
            path = f'/staff/receptions/{row["id"]}'
            before = client.get(f'{path}/operation-context').json()
            command = {'operation_id': str(uuid4()), 'action': action,
                       'expected_revision': row['revision'], 'write_epoch': before['metadata']['write_epoch']}
            result = client.post(f'{path}/operations', json=command, headers=headers)
            assert result.status_code == (409 if outcome == 'rejected' else 200)
            saved = result.json()
            assert saved['outcome'] == outcome
            record = {'operation_id': command['operation_id'], 'reception_id': row['id'],
                      'database_id': before['metadata']['database_id'], 'request': command}
            historical = None
            if action == 'call' and outcome == 'state_changed':
                # The old call receipt must remain a receipt after a later absent.
                current = client.get(f'{path}/operation-context').json()
                absent = {**command, 'operation_id': str(uuid4()), 'action': 'absent',
                          'expected_revision': current['reception']['revision']}
                later = client.post(f'{path}/operations', json=absent, headers=headers)
                assert later.status_code == 200
                historical = {'receipt': saved, 'record': record}
                record = {**record, 'operation_id': absent['operation_id'], 'request': absent}
                before, saved = current, later.json()
            current = client.get(f'{path}/operation-context').json()
            payload = json.dumps({'receipt': saved, 'record': record, 'before': before,
                                  'current': current, 'historical': historical}, allow_nan=False)
            assert TOKEN not in payload and headers['X-CSRF-Token'] not in payload
            checked = subprocess.run([node, '--input-type=module', '-e', NODE_VALIDATOR],
                                     cwd=PROJECT, input=payload, text=True, capture_output=True, timeout=15, check=False)
            assert checked.returncode == 0, checked.stderr
            assert checked.stdout == ''
