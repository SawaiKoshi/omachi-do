import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { createStaffOperations, OPERATION_LOCK_NAME } from '../../web/staff/assets/staff-operations.mjs';
import { createOperationJournal, OPERATION_JOURNAL_KEY, OPERATION_RECOVERY_MS } from '../../web/staff/assets/staff-operation-journal.mjs';
import { createCallJournal, CALL_JOURNAL_KEY } from '../../web/staff/assets/staff-call-journal.mjs';
import { validOperationReceipt, validOperationContext, receiptMatchesCurrent } from '../../web/staff/assets/staff-operation-contract.mjs';

const ID = '7fcb2495-78d3-43ce-b063-7786c86bbf75';
const DB = '57d79c75-ccdd-4225-bf8f-79292b625c78';
const EPOCH = 'a1dbf0b8-b3f8-4e42-b976-fc45b718e47a';
const OTHER = '3660f77e-afb0-49c7-987b-f9eb73d56a25';
const TIME = Date.parse('2026-11-12T09:00:00+09:00');
const STAMP = '2026-11-12T08:10:00+09:00';
const clone = v => JSON.parse(JSON.stringify(v));
const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { resolve, promise }; };
class Storage {
  values = new Map();
  getItem(k) { return this.values.get(k) ?? null; }
  setItem(k, v) { this.values.set(k, v); }
  removeItem(k) { this.values.delete(k); }
}
function locks() {
  const held = new Set(), names = [];
  return { names, async request(name, options, callback) {
    names.push(name); assert.deepEqual(options, { mode: 'exclusive', ifAvailable: true });
    if (held.has(name)) return callback(null);
    held.add(name);
    try { return await callback({ name }); } finally { held.delete(name); }
  } };
}
function fixture(status = 'waiting') {
  const received_at = '2026-11-12T08:00:00+09:00';
  return { metadata: { schema_version: 2, database_id: DB, write_epoch: EPOCH, view_version: 'v1',
    staff_write_mode: 'revision_required', write_enabled: true, seat_corrections_enabled: false, disabled_reason: null },
  reception: { id: ID, display_number: '20261112-1', reception_order: 1, received_at,
    adults: 2, children: 1, total_people: 3, status, called_at: status === 'waiting' ? null : STAMP,
    seated_at: null, cancelled_at: null, actual_wait_minutes: null, revision: 4,
    allowed_actions: status === 'called' ? ['absent', 'seat', 'cancel'] : ['call', 'seat', 'cancel'], valid_seating_event: null,
    snapshot: { captured_at: received_at, source: 'synthetic', waiting_groups: 0, ahead_groups: 0,
      ahead_same_size_groups: 0, party_size_groups: { '1-2': 0, '3-4': 0, '5+': 0 } },
    prediction: { status: 'model_unavailable', minutes: null, model_version: null, predicted_at: received_at, warnings: [] } } };
}
function receipt(row, body, outcome = 'state_changed') {
  const reception = clone(row); delete reception.revision; delete reception.allowed_actions; delete reception.valid_seating_event;
  return { operation_id: body.operation_id, reception_id: row.id, action: body.action, outcome, response_status: 200,
    recorded_at: STAMP, resulting_revision: body.expected_revision + (outcome === 'state_changed' ? 1 : 0), reception, error: null };
}
function response(value, status = 200, actions = 'call,absent') {
  return { status, json: async () => clone(value), headers: { get: name => name === 'X-Omachi-Preview' ? 'synthetic'
    : name === 'X-Omachi-Preview-Actions' ? actions : null } };
}
function input(action = 'call') {
  const operation_id = randomUUID();
  return { operation_id, reception_id: ID, database_id: DB,
    request: { operation_id, action, expected_revision: 4, write_epoch: EPOCH } };
}
function harness(t, options = {}) {
  const storage = options.storage ?? new Storage(), lockManager = options.locks ?? locks();
  const data = fixture(options.status), ledger = options.ledger ?? new Map(), requests = [];
  const auth = { active: true, ready: true, csrf: 'synthetic-memory-only', version: 1 };
  let authFailures = 0, issuedIds = 0;
  let time = TIME, contexts = 0;
  const env = { location: { protocol: 'https:', hostname: 'staff.localhost' },
    navigator: { locks: lockManager }, sessionStorage: storage, crypto: { randomUUID() { issuedIds++; return randomUUID(); } }, AbortController,
    setTimeout, clearTimeout, ...options.env };
  const client = createStaffOperations({ window: env, security: () => auth, now: () => time,
    ...(Object.hasOwn(options, 'authFailure')
      ? options.authFailure === undefined ? {} : { authFailure: () => { authFailures++; return options.authFailure(auth); } }
      : { authFailure: () => { authFailures++; auth.active = false; auth.version++; } }),
    fetchImpl: async (url, request) => {
      requests.push({ url, ...request });
      if (url.endsWith('/operation-context')) {
        contexts++;
        const override = await options.context?.(contexts, data, request);
        return override ?? response(data, 200, options.actions);
      }
      if (request.method === 'POST') {
        const body = JSON.parse(request.body);
        const saved = JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY));
        assert.deepEqual(body, saved.request, 'POST requires verified persisted fixed body');
        assert.equal(request.headers['X-CSRF-Token'], auth.csrf);
        if (!ledger.has(body.operation_id)) {
          data.reception.status = body.action === 'call' ? 'called' : 'absent';
          data.reception.revision = body.expected_revision + 1;
          data.reception.allowed_actions = body.action === 'call' ? ['absent', 'seat', 'cancel'] : ['call', 'seat', 'cancel'];
          if (body.action === 'call') data.reception.called_at = STAMP;
          ledger.set(body.operation_id, receipt(data.reception, body));
        }
        const override = await options.post?.(body, data, ledger, request);
        return override ?? response(ledger.get(body.operation_id), 200, options.actions);
      }
      const override = await options.lookup?.(data, ledger, request);
      return override ?? response(ledger.get(url.split('/').at(-1)) ?? {}, ledger.has(url.split('/').at(-1)) ? 200 : 404, options.actions);
    } });
  t.after(() => client.stop());
  return { client, data, ledger, storage, requests, auth, env, authFailures: () => authFailures, issuedIds: () => issuedIds, setTime: v => { time = v; },
    posts: () => requests.filter(r => r.method === 'POST'),
    async execute(action = 'call') { return client.confirm(await client.prepare(action, ID)); } };
}

for (const action of ['call', 'absent']) {
  test(`${action}: confirm twice sends once, validates the saved receipt and separately reads current context`, async t => {
    const h = harness(t, { status: action === 'call' ? 'waiting' : 'called' });
    const before = clone(h.data.reception);
    const ticket = await h.client.prepare(action, ID);
    const [a, b] = await Promise.all([h.client.confirm(ticket), h.client.confirm(ticket)]);
    assert.deepEqual([a, b], [true, false]);
    assert.equal(h.posts().length, 1);
    const state = h.client.status();
    assert.equal(state.journal.kind, 'empty');
    assert.equal(state.phase, 'resolved');
    assert.equal(state.receipt.action, action);
    assert.equal(state.current.reception.status, action === 'call' ? 'called' : 'absent');
    if (action === 'absent') assert.equal(state.receipt.reception.called_at, before.called_at);
    assert.ok(h.requests.filter(r => r.url.endsWith('operation-context')).length >= 3);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null);
  });
  test(`lost ${action} response blocks both actions and can be recovered without a second POST`, async t => {
    const h = harness(t, { status: action === 'call' ? 'waiting' : 'called', post: () => { throw Error('lost'); } });
    assert.equal(await h.execute(action), false);
    const saved = h.client.status().journal.record;
    for (const a of ['call', 'absent']) assert.equal(await h.client.prepare(a, ID), null);
    assert.equal(h.client.status().phase, 'unknown');
    assert.equal(await h.client.lookup(), true);
    assert.equal(h.client.status().receipt.operation_id, saved.operation_id);
    assert.equal(h.posts().length, 1);
  });
}

test('call then absent reuse the same slot and preserve the original reception data', async t => {
  const h = harness(t);
  assert.equal(await h.execute(), true);
  const call = h.client.status().receipt;
  assert.equal(await h.execute('absent'), true);
  const absent = h.client.status().receipt;
  assert.notEqual(call.operation_id, absent.operation_id);
  assert.deepEqual(absent.reception, { ...call.reception, status: 'absent' });
  assert.equal(h.posts().length, 2);
});

for (const scenario of ['404', 'failure', 'generic409', 'wrongAction', 'malformed']) {
  test(`lookup ${scenario} keeps the exact pending identity and never treats HTTP200 as operation success`, async t => {
    const h = harness(t, { post: () => { throw Error('lost'); }, lookup: (_, ledger) => {
      if (scenario === 'failure') throw Error('offline');
      if (scenario === '404') return response({}, 404);
      if (scenario === 'generic409') return response({ detail: 'conflict' }, 200);
      const value = clone([...ledger.values()][0]);
      if (scenario === 'wrongAction') value.action = 'absent'; else value.response_status = 409;
      return response(value);
    } });
    await h.execute(); const saved = h.storage.getItem(OPERATION_JOURNAL_KEY);
    assert.equal(await h.client.lookup(), false);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), saved);
    assert.equal(h.client.status().journal.kind, 'pending');
    assert.equal(h.posts().length, 1);
  });
}

test('explicit retry uses the same ID, action, revision and body after current revision advances', async t => {
  let posts = 0;
  const h = harness(t, { lookup: () => response({}, 404), post: () => { if (++posts === 1) throw Error('lost'); } });
  await h.execute();
  h.data.reception.status = 'absent'; h.data.reception.revision += 1;
  assert.equal(await h.client.lookup(), false);
  assert.equal(await h.client.retry(), true);
  assert.equal(h.posts()[0].body, h.posts()[1].body);
  const state = h.client.status();
  assert.equal(state.receipt.reception.status, 'called');
  assert.equal(state.current.reception.status, 'absent');
});

test('reloading and relogging require lookup first and use only the new in-memory CSRF for fixed retry', async t => {
  const first = harness(t, { post: () => { throw Error('lost'); } });
  await first.execute(); first.client.stop(); await tick();
  const h = harness(t, { storage: first.storage, ledger: first.ledger, lookup: () => response({}, 404) });
  h.auth.version = 2; h.auth.csrf = 'synthetic-new-memory-csrf';
  h.data.reception = clone(first.data.reception);
  assert.equal(await h.client.retry(), false);
  assert.equal(await h.client.lookup(), false);
  assert.equal(await h.client.retry(), true);
  assert.equal(h.posts()[0].body, first.posts()[0].body);
  assert.equal(JSON.stringify([...h.storage.values]).includes('csrf'), false);
});

test('a received receipt with unreadable current state blocks retry until lookup resolves it', async t => {
  let broken = true;
  const h = harness(t, { context: n => n >= 3 && broken ? response({}, 503) : undefined });
  assert.equal(await h.execute(), false);
  assert.equal(h.client.status().phase, 'current_unverified');
  assert.equal(h.client.status().receipt.outcome, 'state_changed');
  assert.equal(await h.client.retry(), false);
  broken = false;
  assert.equal(await h.client.lookup(), true);
  assert.equal(h.posts().length, 1);
});

for (const change of [d => { d.reception.revision++; }, d => { d.metadata.write_epoch = OTHER; },
  d => { d.metadata.database_id = OTHER; }, d => { d.reception.allowed_actions = []; }, d => { d.reception.total_people = 9; }]) {
  test(`changes between confirmation and POST invalidate confirmation: ${change}`, async t => {
    const h = harness(t); const ticket = await h.client.prepare('call', ID);
    change(h.data);
    assert.equal(await h.client.confirm(ticket), false);
    assert.equal(h.posts().length, 0);
  });
}

for (const action of ['call', 'absent']) {
  for (const status of ['waiting', 'called', 'absent', 'seated', 'cancelled']) {
    test(`${action} new-operation source state ${status} follows its own transition contract`, async t => {
      const h = harness(t, { status }); h.data.reception.allowed_actions = ['call', 'absent'];
      const allowed = action === 'call' ? ['waiting', 'absent'].includes(status) : status === 'called';
      assert.equal(Boolean(await h.client.prepare(action, ID)), allowed);
      assert.equal(h.posts().length, 0);
    });
  }
}

for (const options of [{ env: { navigator: {} } }, { locks: { request: async (_, __, cb) => cb(null) } },
  { locks: { request: async () => { throw Error('unavailable'); } } }, { env: { location: { protocol: 'http:', hostname: 'staff.localhost' } } },
  { env: { sessionStorage: null } }, { env: { sessionStorage: { getItem() { throw Error('denied'); } } } }]) {
  test(`unavailable lock, insecure origin or unusable storage forbids POST (${JSON.stringify(options)})`, async t => {
    const h = harness(t, options); await tick();
    assert.equal(await h.execute(), false);
    assert.equal(h.posts().length, 0);
  });
}

for (const fault of ['throw', 'discard', 'modify']) {
  test(`storage ${fault} after preparation never permits POST`, async t => {
    const h = harness(t); const ticket = await h.client.prepare('call', ID);
    const set = h.storage.setItem.bind(h.storage);
    h.storage.setItem = (k, v) => { if (k === OPERATION_JOURNAL_KEY) {
      if (fault === 'throw') throw Error('quota'); if (fault === 'discard') return; v += ' ';
    } set(k, v); };
    assert.equal(await h.client.confirm(ticket), false);
    assert.equal(h.posts().length, 0);
    assert.equal(h.client.status().journal.kind, 'blocked');
  });
}

test('same-origin tabs and the old call lock contend on exactly the same lock name', async t => {
  const manager = locks(), gate = deferred();
  const old = manager.request('omachi.synthetic.staff.call.v1', { mode: 'exclusive', ifAvailable: true }, async () => gate.promise);
  const blocked = harness(t, { locks: manager, status: 'called' });
  assert.equal(blocked.client.status().lockState, 'other');
  assert.equal(await blocked.execute('absent'), false);
  gate.resolve(); await old;
  const owner = harness(t, { locks: manager });
  const other = harness(t, { locks: manager, status: 'called' });
  const [call, absent] = await Promise.all([owner.execute('call'), other.execute('absent')]);
  assert.deepEqual([call, absent], [true, false]);
  assert.equal(other.posts().length, 0);
  assert.ok(manager.names.every(name => name === OPERATION_LOCK_NAME));
});

test('a tab without the lock may still query and resolve its existing pending record', async t => {
  const original = harness(t, { post: () => { throw Error('lost'); } }); await original.execute();
  const h = harness(t, { storage: original.storage, ledger: original.ledger, locks: { request: async (_, __, cb) => cb(null) } });
  h.data.reception = clone(original.data.reception);
  assert.equal(await h.client.lookup(), true);
  assert.equal(h.posts().length, 0);
});

for (const operation of ['post', 'lookup']) {
  test(`${operation} in flight survives repeated background reads and rejects any competing operation`, async t => {
    const gate = deferred(), entered = deferred(); let signal;
    const options = operation === 'post' ? { post: async (_, __, ___, request) => { signal = request.signal; entered.resolve(); return gate.promise; } }
      : { post: () => { throw Error('lost'); }, lookup: async (_, __, request) => { signal = request.signal; entered.resolve(); return gate.promise; } };
    const h = harness(t, options);
    let pending;
    if (operation === 'post') pending = h.execute();
    else { await h.execute(); pending = h.client.lookup(); }
    await entered.promise;
    assert.ok(signal);
    for (let i = 0; i < 10; i++) {
      assert.equal(h.client.status().busy, true);
      assert.equal(await h.client.prepare('absent', ID), null);
      assert.equal(await h.client.lookup(), false);
    }
    assert.equal(signal.aborted, false);
    gate.resolve(response([...h.ledger.values()][0]));
    assert.equal(await pending, true);
    assert.equal(h.posts().length, 1);
  });
}

for (const elapsed of [OPERATION_RECOVERY_MS, -1]) {
  test(`expired or reversed clock (${elapsed}) retains a queryable manual marker`, async t => {
    const h = harness(t, { post: () => { throw Error('lost'); } }); await h.execute();
    h.setTime(TIME + elapsed);
    const marker = h.client.status().journal;
    assert.equal(marker.kind, 'manual'); assert.equal(marker.record.action, 'call');
    assert.equal(marker.record.request, undefined);
    assert.equal(await h.client.retry(), false);
    assert.equal(await h.client.lookup(), true);
    assert.equal(h.posts().length, 1);
  });
}

for (const field of ['database_id', 'write_epoch']) {
  test(`changed ${field} prevents fixed retry and new operations`, async t => {
    const h = harness(t, { post: () => { throw Error('lost'); } }); await h.execute();
    h.data.metadata[field] = OTHER;
    assert.equal(await h.client.lookup(), false);
    assert.equal(await h.client.retry(), false);
    assert.equal(await h.client.prepare('absent', ID), null);
    assert.equal(h.posts().length, 1);
    assert.equal(h.client.status().journal.kind, 'pending');
  });
}

for (const manual of [false, true]) {
  test(`legacy ${manual ? 'manual' : 'pending'} receipt is queried as call and resolved in place before absent can start`, async t => {
    const storage = new Storage(), command = input();
    createCallJournal({ storage, now: () => TIME }).begin(command);
    const h = harness(t, { storage, status: 'called' });
    h.data.reception.revision = 5;
    h.ledger.set(command.operation_id, receipt(h.data.reception, command.request));
    if (manual) h.setTime(TIME + OPERATION_RECOVERY_MS);
    assert.equal(await h.client.prepare('absent', ID), null);
    assert.equal(await h.client.retry(), false);
    assert.equal(await h.client.lookup(), true);
    assert.equal(storage.getItem(CALL_JOURNAL_KEY), null);
    assert.equal(h.client.status().receipt.action, 'call');
    assert.ok(await h.client.prepare('absent', ID));
    assert.equal(h.posts().length, 0);
  });
}

for (const outcome of ['no_change', 'rejected']) {
  test(`lookup HTTP200 resolves saved ${outcome} as its own outcome`, async t => {
    const storage = new Storage(), command = input('absent');
    createOperationJournal({ storage, now: () => TIME }).begin(command);
    const h = harness(t, { storage, status: 'absent' });
    const saved = outcome === 'no_change' ? receipt(h.data.reception, command.request, outcome) : {
      operation_id: command.operation_id, reception_id: ID, action: 'absent', outcome,
      response_status: 409, recorded_at: STAMP, resulting_revision: 4, reception: null,
      error: { code: 'state_conflict', detail: 'synthetic rejection' } };
    h.ledger.set(command.operation_id, saved);
    assert.equal(await h.client.lookup(), true);
    assert.equal(h.client.status().phase, outcome);
    assert.equal(h.client.status().receipt.response_status, outcome === 'rejected' ? 409 : 200);
    assert.equal(h.posts().length, 0);
  });
}

test('logout during POST never settles a late response or creates another ID', async t => {
  const gate = deferred(), entered = deferred();
  const h = harness(t, { post: () => { entered.resolve(); return gate.promise; } });
  const sending = h.execute(); await entered.promise;
  h.auth.active = false; h.auth.version++;
  gate.resolve(response([...h.ledger.values()][0]));
  assert.equal(await sending, false);
  assert.equal(h.client.status().journal.kind, 'pending');
  h.auth.active = true;
  assert.equal(await h.client.retry(), false);
  assert.equal(await h.client.lookup(), true);
  assert.equal(h.posts().length, 1);
});

for (const actions of ['call', 'absent', 'call,absent,seat', 'absent,call', '']) {
  test(`response capability ${JSON.stringify(actions)} cannot activate the two-action foundation`, async t => {
    const h = harness(t, { actions });
    assert.equal(await h.execute(), false);
    assert.equal(h.posts().length, 0);
  });
}

test('absent validates called_at, stable reception data, snapshot and prediction without relaxing call', () => {
  const before = fixture('called').reception, command = input('absent');
  const result = receipt({ ...before, status: 'absent' }, command.request);
  assert.equal(validOperationReceipt(result, command, before), true);
  for (const mutate of [r => { r.action = 'call'; }, r => { r.reception.status = 'called'; },
    r => { r.reception.called_at = null; }, r => { r.reception.called_at = '2026-11-12T08:15:00+09:00'; },
    r => { r.reception.received_at = STAMP; }, r => { r.reception.reception_order++; },
    r => { r.reception.snapshot.ahead_groups++; }, r => { r.reception.prediction.warnings.push('changed'); },
    r => { r.resulting_revision++; }, r => { r.reception.seated_at = STAMP; },
    r => { r.reception.actual_wait_minutes = 10; }, r => { r.reception.extra = true; }]) {
    const bad = clone(result); mutate(bad);
    assert.equal(validOperationReceipt(bad, command, before), false, String(mutate));
  }
  assert.equal(validOperationReceipt(result, { ...command, request: { ...command.request, action: 'call' } }), false);
});

test('old successful receipts cannot roll back later state or resolve a regressed/inconsistent context', () => {
  const before = fixture('called').reception, command = input('absent');
  const result = receipt({ ...before, status: 'absent' }, command.request);
  assert.equal(receiptMatchesCurrent(result, { ...before, status: 'waiting', revision: 6 }), true);
  assert.equal(receiptMatchesCurrent(result, { ...before, status: 'waiting', revision: 5 }), false);
  assert.equal(receiptMatchesCurrent(result, { ...before, status: 'absent', revision: 4 }), false);
  assert.equal(receiptMatchesCurrent(result, { ...before, status: 'absent', revision: 5 }), true);
  assert.equal(validOperationContext(fixture(), ID), true);
  const invalid = fixture(); invalid.metadata.seat_corrections_enabled = true;
  assert.equal(validOperationContext(invalid, ID), false);
});

test('timeout aborts only its own request and retains the pending operation for lookup', async t => {
  const entered = deferred(); let signal, expire; let timer = 0;
  const h = harness(t, { env: { setTimeout: callback => { expire = callback; return ++timer; }, clearTimeout() {} },
    post: (_, __, ___, request) => { signal = request.signal; entered.resolve(); return new Promise(() => {}); } });
  const sending = h.execute(); await entered.promise;
  expire();
  assert.equal(await sending, false);
  assert.equal(signal.aborted, true);
  assert.equal(h.client.status().journal.kind, 'pending');
  assert.equal(await h.client.prepare('absent', ID), null);
  assert.equal(await h.client.lookup(), true);
  assert.equal(h.posts().length, 1);
});

test('generic POST409 remains unknown and never refreshes the fixed revision for retry', async t => {
  const h = harness(t, { post: () => response({ detail: 'not a receipt' }, 409), lookup: () => response({}, 404) });
  assert.equal(await h.execute(), false);
  const fixed = h.posts()[0].body;
  assert.equal(h.client.status().journal.kind, 'pending');
  assert.equal(await h.client.lookup(), false);
  assert.equal(await h.client.retry(), false);
  assert.equal(h.posts()[1].body, fixed);
});

for (const status of [401, 403]) {
  test(`authentication ${status} keeps the operation and drops access to stale display data`, async t => {
    const h = harness(t, { post: () => response({}, status) });
    assert.equal(await h.execute(), false);
    assert.equal(h.authFailures(), 1);
    assert.equal(h.client.status().journal.kind, 'pending');
    assert.equal(h.client.status().receipt, null);
    assert.equal(h.client.status().current, null);
    assert.equal(await h.client.retry(), false);
  });
}

test('logout and a different login do not expose previously resolved reception details', async t => {
  const h = harness(t); await h.execute();
  assert.ok(h.client.status().current);
  h.auth.active = false; h.auth.version++;
  assert.equal(h.client.status().current, null);
  assert.equal(h.client.status().receipt, null);
  h.auth.active = true;
  assert.equal(h.client.status().current, null);
  assert.equal(h.client.status().receipt, null);
});

test('an old absent receipt resolves against a later waiting context without applying the old status', async t => {
  const h = harness(t, { status: 'called', post: () => { throw Error('lost'); } });
  await h.execute('absent');
  h.data.reception.status = 'waiting'; h.data.reception.revision++;
  assert.equal(await h.client.lookup(), true);
  assert.equal(h.client.status().receipt.reception.status, 'absent');
  assert.equal(h.client.status().current.reception.status, 'waiting');
  assert.equal(h.data.reception.status, 'waiting');
});

test('port-separated origins have independent locks and journals; cross-port exclusion is not provided', async t => {
  const a = harness(t, { env: { location: { protocol: 'https:', hostname: 'staff.localhost', port: '8444' } } });
  const b = harness(t, { status: 'called', env: { location: { protocol: 'https:', hostname: 'staff.localhost', port: '8445' } } });
  // Two origin-specific managers intentionally allow both; the production UI
  // remains disconnected, and the labs must still be used one at a time.
  assert.equal(await a.execute(), true);
  assert.equal(await b.execute('absent'), true);
  assert.notEqual(a.storage, b.storage);
});

test('a copied/forged confirmation ticket is not an execution capability', async t => {
  const h = harness(t); const ticket = await h.client.prepare('call', ID);
  assert.equal(await h.client.confirm(clone(ticket)), false);
  assert.equal(h.posts().length, 0);
  assert.equal(await h.client.confirm(ticket), true);
});

// P2 regression: authentication closure is an internal guarantee, even when
// the caller supplies no notification handler or a broken/asynchronous one.
for (const status of [401, 403]) {
  for (const handler of ['omitted', 'noop', 'async', 'throw', 'reject', 'changeVersion']) {
    test(`P2 ${status}/${handler}: stale displays and confirmation close before notification`, async t => {
      let expired = false, h, notificationState;
      const notification = deferred();
      const callback = handler === 'omitted' ? undefined : auth => {
        const state = h.client.status();
        notificationState = { authExpired: state.authExpired, current: state.current, receipt: state.receipt };
        if (handler === 'throw') throw Error('synthetic callback failure');
        if (handler === 'reject') return Promise.reject(Error('synthetic async callback failure'));
        if (handler === 'async') return notification.promise;
        if (handler === 'changeVersion') { auth.version++; auth.active = true; auth.ready = true; }
      };
      h = harness(t, { authFailure: callback, context: () => expired ? response({}, status) : undefined });
      assert.equal(await h.execute(), true);
      assert.ok(h.client.status().current);
      const ticket = await h.client.prepare('absent', ID);
      assert.ok(ticket.context);
      expired = true;
      assert.equal(await h.client.confirm(ticket), false);
      assert.equal(h.client.status().authExpired, true);
      assert.equal(h.client.status().phase, 'auth_expired');
      assert.equal(h.client.status().current, null);
      assert.equal(h.client.status().receipt, null);
      assert.equal(ticket.context === null, true);
      assert.equal(h.authFailures(), handler === 'omitted' ? 0 : 1);
      if (handler !== 'omitted') assert.deepEqual(notificationState, { authExpired: true, current: null, receipt: null });
      const count = h.requests.length;
      for (const action of ['call', 'absent']) assert.equal(await h.client.prepare(action, ID), null);
      assert.equal(await h.client.confirm(ticket), false);
      assert.equal(await h.client.lookup(), false);
      assert.equal(await h.client.retry(), false);
      assert.equal(h.requests.length, count);
      assert.equal(h.issuedIds(), 1);
      assert.equal(h.posts().length, 1);
      // A callback completion or externally changed auth flags cannot reopen
      // this instance. Recovery deliberately requires a new instance.
      notification.resolve(); await tick();
      h.auth.version++; h.auth.active = true; h.auth.ready = true;
      assert.equal(await h.execute('absent'), false);
      assert.equal(h.client.status().authExpired, true);
      assert.equal(h.client.status().current, null);
      assert.equal(h.issuedIds(), 1);
    });
  }
}

for (const status of [401, 403]) {
  for (const action of ['call', 'absent']) {
    for (const stage of ['post', 'binding', 'lookup', 'current']) {
      test(`${status} in ${action}/${stage} closes without changing the uncertain fixed journal`, async t => {
        let fixed, bodyReads = 0;
        const denied = () => ({ status, headers: { get: () => null }, json() { bodyReads++; throw Error('must not read denied body'); } });
        const h = harness(t, { status: action === 'call' ? 'waiting' : 'called', authFailure: undefined,
          context: n => n === 3 && ['binding', 'current'].includes(stage) ? denied() : undefined,
          post: (_, __, ___, request) => {
            fixed = request.body;
            if (stage === 'post') return denied();
            if (stage !== 'current') throw Error('synthetic lost response');
          }, lookup: () => stage === 'lookup' ? denied() : undefined });
        assert.equal(await h.execute(action), false);
        if (['binding', 'lookup'].includes(stage)) assert.equal(await h.client.lookup(), false);
        const state = h.client.status();
        assert.equal(state.authExpired, true);
        assert.equal(state.phase, 'auth_expired');
        assert.equal(state.receipt, null); assert.equal(state.current, null);
        assert.equal(state.journal.kind, 'pending');
        assert.equal(JSON.stringify(JSON.parse(h.storage.getItem(OPERATION_JOURNAL_KEY)).request), fixed);
        const saved = new Map(h.storage.values), requests = h.requests.length;
        for (const next of ['call', 'absent']) assert.equal(await h.execute(next), false);
        assert.equal(await h.client.lookup(), false); assert.equal(await h.client.retry(), false);
        assert.equal(h.requests.length, requests); assert.deepEqual(h.storage.values, saved);
        assert.equal(h.posts().length, 1); assert.equal(h.issuedIds(), 1);
        assert.equal(bodyReads, 0);
      });
    }
  }
}

for (const status of [401, 403]) {
  for (const throwing of [false, true]) {
    test(`${status}: failing notification cannot erase the uncertain operation (${throwing ? 'throw' : 'reject'})`, async t => {
      const h = harness(t, { post: () => response({}, status), authFailure: () => {
        if (throwing) throw Error('synthetic notification failure');
        return Promise.reject(Error('synthetic asynchronous notification failure'));
      } });
      assert.equal(await h.execute(), false); await tick();
      assert.equal(h.client.status().authExpired, true);
      assert.equal(h.client.status().journal.kind, 'pending');
      assert.equal(h.client.status().receipt, null);
      assert.equal(await h.execute('absent'), false);
      assert.equal(h.issuedIds(), 1); assert.equal(h.posts().length, 1);
    });
  }
}

for (const status of [401, 403]) {
  for (const stage of ['headers', 'body', 'current-body']) {
    test(`background ${status} followed by late 200 ${stage} cannot restore display or settle the journal`, async t => {
      const gate = deferred(), entered = deferred(); let h;
      h = harness(t, { authFailure: undefined,
        post: (_, __, ledger) => {
          if (stage === 'current-body') return;
          const value = [...ledger.values()][0];
          const result = response(value);
          if (stage === 'headers') { entered.resolve(); return gate.promise; }
          result.json = () => { entered.resolve(); return gate.promise; };
          return result;
        }, context: n => {
          if (stage !== 'current-body' || n !== 3) return;
          const result = response(h.data);
          result.json = () => { entered.resolve(); return gate.promise; };
          return result;
        } });
      const pending = h.execute(); await entered.promise;
      const saved = new Map(h.storage.values);
      // Internal operations are serialized by busy. An outside session/list
      // poll can run concurrently and must notify the foundation of a denial.
      const poll = Promise.resolve(response({}, status)).then(result => {
        if ([401, 403].includes(result.status)) h.client.expireAuthentication();
      });
      await poll;
      assert.equal(h.client.status().authExpired, true);
      assert.equal(h.client.status().receipt, null); assert.equal(h.client.status().current, null);
      h.auth.version++; h.auth.active = true; h.auth.ready = true;
      const receipt200 = [...h.ledger.values()][0];
      gate.resolve(stage === 'headers' ? response(receipt200) : stage === 'body' ? receipt200 : clone(h.data));
      assert.equal(await pending, false);
      assert.equal(h.client.status().phase, 'auth_expired');
      assert.equal(h.client.status().receipt, null); assert.equal(h.client.status().current, null);
      assert.deepEqual(h.storage.values, saved);
      assert.equal(await h.execute('absent'), false);
      assert.equal(h.posts().length, 1); assert.equal(h.issuedIds(), 1);
    });
  }
}

test('explicit expiry revokes a retained confirmation ticket and is idempotent', async t => {
  const h = harness(t, { authFailure: () => {} });
  const ticket = await h.client.prepare('call', ID);
  assert.ok(ticket.context);
  h.client.expireAuthentication(); h.client.expireAuthentication();
  assert.equal(ticket.context, null);
  assert.equal(h.authFailures(), 1);
  assert.equal(await h.client.confirm(ticket), false);
  assert.equal(h.issuedIds(), 0); assert.equal(h.posts().length, 0);
});

for (const action of ['call', 'absent']) {
  test(`reauthenticated replacement queries the existing ${action} ID without auto-posting`, async t => {
    const manager = locks();
    const first = harness(t, { status: action === 'call' ? 'waiting' : 'called', locks: manager,
      authFailure: undefined, post: () => response({}, 401) });
    assert.equal(await first.execute(action), false);
    const stored = first.client.status().journal.record;
    const saved = new Map(first.storage.values);
    first.client.stop(); await tick();
    // The browser owner must first verify GET /staff/session and supply the
    // new active session/version/CSRF. Context reads then verify cookie + DB.
    const replacement = harness(t, { storage: first.storage, ledger: first.ledger, locks: manager });
    replacement.auth.version = 2; replacement.auth.csrf = 'synthetic-reauthenticated-csrf';
    replacement.data.reception = clone(first.data.reception);
    assert.equal(replacement.client.status().receipt, null);
    assert.equal(replacement.client.status().current, null);
    assert.equal(await replacement.execute('call'), false);
    assert.equal(await replacement.execute('absent'), false);
    assert.equal(await replacement.client.retry(), false);
    assert.deepEqual(replacement.storage.values, saved);
    assert.equal(replacement.requests.length, 0);
    assert.equal(await replacement.client.lookup(), true);
    const lookups = replacement.requests.filter(r => r.url.startsWith('/staff/operations/'));
    assert.equal(lookups.length, 1); assert.ok(lookups[0].url.endsWith(stored.operation_id));
    assert.equal(replacement.client.status().receipt.operation_id, stored.operation_id);
    assert.equal(replacement.client.status().receipt.action, action);
    assert.equal(replacement.client.status().current.metadata.database_id, stored.database_id);
    assert.equal(replacement.client.status().current.metadata.write_epoch, stored.request.write_epoch);
    assert.equal(replacement.client.status().journal.kind, 'empty');
    assert.equal(replacement.posts().length, 0); assert.equal(replacement.issuedIds(), 0);
    assert.equal(first.client.status().current, null);
  });
}

for (const status of [401, 403]) {
  test(`recreating without a valid session fails closed again (${status})`, async t => {
    const storage = new Storage();
    createOperationJournal({ storage, now: () => TIME }).begin(input());
    const h = harness(t, { storage, authFailure: undefined, context: () => response({}, status) });
    const saved = new Map(storage.values);
    assert.equal(await h.client.lookup(), false);
    assert.equal(h.client.status().authExpired, true);
    assert.deepEqual(storage.values, saved);
    assert.equal(h.posts().length, 0); assert.equal(h.issuedIds(), 0);
  });
}

for (const field of ['database_id', 'write_epoch']) {
  test(`replacement after expiry cannot settle or post against a changed ${field}`, async t => {
    const first = harness(t, { authFailure: undefined, post: () => response({}, 403) });
    await first.execute(); first.client.stop(); await tick();
    const next = harness(t, { storage: first.storage, ledger: first.ledger });
    next.auth.version = 2;
    next.data.reception = clone(first.data.reception); next.data.metadata[field] = OTHER;
    assert.equal(await next.client.lookup(), false);
    assert.equal(next.client.status().journal.kind, 'pending');
    assert.equal(await next.client.retry(), false);
    assert.equal(await next.execute('absent'), false);
    assert.equal(next.posts().length, 0); assert.equal(next.issuedIds(), 0);
  });
}

for (const manual of [false, true]) {
  test(`legacy ${manual ? 'manual' : 'pending'} survives 401 and recovers by lookup in a fresh instance`, async t => {
    const storage = new Storage(), command = input();
    createCallJournal({ storage, now: () => TIME }).begin(command);
    const first = harness(t, { storage, status: 'called', authFailure: undefined, lookup: () => response({}, 401) });
    first.data.reception.revision = 5;
    first.ledger.set(command.operation_id, receipt(first.data.reception, command.request));
    if (manual) first.setTime(TIME + OPERATION_RECOVERY_MS);
    assert.equal(first.client.status().journal.source, 'legacy');
    const saved = new Map(storage.values);
    assert.equal(await first.client.lookup(), false);
    assert.equal(first.client.status().authExpired, true);
    assert.deepEqual(storage.values, saved);
    first.client.stop(); await tick();
    const next = harness(t, { storage, ledger: first.ledger, status: 'called' });
    next.auth.version = 2; next.data.reception.revision = 5;
    if (manual) next.setTime(TIME + OPERATION_RECOVERY_MS);
    assert.equal(await next.client.retry(), false);
    assert.equal(await next.client.lookup(), true);
    assert.equal(next.client.status().receipt.operation_id, command.operation_id);
    assert.equal(next.client.status().receipt.action, 'call');
    assert.equal(next.posts().length, 0); assert.equal(next.issuedIds(), 0);
  });
}

test('a stopped instance cannot invalidate a recovered replacement with a late 401', async t => {
  const gate = deferred(), entered = deferred(), manager = locks();
  const first = harness(t, { locks: manager, post: () => { entered.resolve(); return gate.promise; } });
  const pending = first.execute(); await entered.promise;
  first.client.stop(); await tick();
  const next = harness(t, { storage: first.storage, ledger: first.ledger, locks: manager });
  next.auth.version = 2; next.data.reception = clone(first.data.reception);
  assert.equal(await next.client.lookup(), true);
  gate.resolve(response({}, 401));
  assert.equal(await pending, false);
  assert.equal(first.authFailures(), 0);
  assert.equal(next.client.status().authExpired, false);
  assert.ok(next.client.status().current);
});
