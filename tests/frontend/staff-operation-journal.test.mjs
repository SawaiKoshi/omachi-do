import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createOperationJournal, OPERATION_JOURNAL_KEY, OPERATION_JOURNAL_GUARD_KEY, OPERATION_RECOVERY_MS,
} from '../../web/staff/assets/staff-operation-journal.mjs';

const OP = '1b7c9370-d816-427b-a985-1665a8fd098b';
const OTHER = '3660f77e-afb0-49c7-987b-f9eb73d56a25';
const RECEPTION = '7fcb2495-78d3-43ce-b063-7786c86bbf75';
const DATABASE = '57d79c75-ccdd-4225-bf8f-79292b625c78';
const EPOCH = 'a1dbf0b8-b3f8-4e42-b976-fc45b718e47a';
const NIL = '00000000-0000-0000-0000-000000000000';
const START = Date.parse('2026-11-10T09:00:00+09:00');
const clone = value => JSON.parse(JSON.stringify(value));
const input = (changes = {}) => ({
  operation_id: OP, reception_id: RECEPTION, database_id: DATABASE,
  request: { operation_id: OP, action: 'call', expected_revision: 4, write_epoch: EPOCH },
  ...changes,
});
const proof = (changes = {}) => ({ operation_id: OP, reception_id: RECEPTION, database_id: DATABASE, ...changes });
const environment = (changes = {}) => ({ database_id: DATABASE, write_epoch: EPOCH, ...changes });

class MemoryStorage {
  constructor() { this.values = new Map(); this.reads = 0; this.writes = 0; }
  getItem(key) { this.reads += 1; return this.values.get(key) ?? null; }
  setItem(key, value) { this.writes += 1; this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
  clear() { this.values.clear(); }
}

function setup() {
  const storage = new MemoryStorage();
  let time = START;
  const journal = createOperationJournal({ storage, now: () => time });
  return { storage, journal, setTime: value => { time = value; },
    reload: () => createOperationJournal({ storage, now: () => time }) };
}

test('module is inert until an explicitly injected journal is read', () => {
  const { storage, journal } = setup();
  assert.equal(storage.reads, 0);
  assert.equal(storage.writes, 0);
  assert.deepEqual(journal.read(), { kind: 'empty' });
  assert.equal(storage.getItem(OPERATION_JOURNAL_GUARD_KEY), 'empty');
  assert.equal(storage.getItem(OPERATION_JOURNAL_KEY), null);
});

test('before sending, one fixed minimal request is written and read back', () => {
  const { storage, journal } = setup();
  const record = journal.begin(input());
  assert.equal(record.state, 'pending');
  assert.equal(record.expires_at - record.created_at, OPERATION_RECOVERY_MS);
  assert.deepEqual(record.request, input().request);
  assert.equal(storage.getItem(OPERATION_JOURNAL_GUARD_KEY), `pending:${OP}`);
  assert.deepEqual(Object.keys(record).sort(), ['version', 'state', 'operation_id', 'reception_id',
    'database_id', 'action', 'request', 'created_at', 'expires_at'].sort());
  assert.deepEqual(JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY)), record);
  assert.equal(Object.isFrozen(record), true);
  assert.equal(Object.isFrozen(record.request), true);
});

test('reloading and signing in again preserve the same ID, body and creation time', () => {
  const { journal, reload } = setup();
  const saved = journal.begin(input());
  const recovered = reload();
  assert.deepEqual(recovered.read(), { kind: 'pending', source: 'v2', record: saved });
  assert.equal(JSON.stringify(recovered.requestForRetry(environment())), JSON.stringify(saved.request));
  assert.equal(recovered.begin(input({ operation_id: OTHER })), null);
});

test('call body has canonical serialization regardless of caller property order', () => {
  const { journal, reload } = setup();
  journal.begin(input({ request: { write_epoch: EPOCH, expected_revision: 4, action: 'call', operation_id: OP } }));
  assert.equal(JSON.stringify(reload().requestForRetry(environment())), JSON.stringify(input().request));
});

test('caller cannot mutate the saved request or replace the unresolved operation', () => {
  const { journal } = setup();
  const source = input();
  journal.begin(source);
  source.request.expected_revision = 100;
  assert.equal(journal.read().record.request.expected_revision, 4);
  assert.throws(() => { journal.read().record.request.expected_revision = 99; }, TypeError);
  assert.equal(journal.begin(input()), null);
  assert.equal(journal.begin(input({ operation_id: OTHER,
    request: { ...input().request, operation_id: OTHER } })), null);
  assert.equal(journal.read().record.operation_id, OP);
});

for (const changes of [
  { database_id: OTHER }, { write_epoch: OTHER }, { database_id: NIL },
  { write_epoch: NIL }, { database_id: DATABASE, write_epoch: EPOCH, extra: true },
]) {
  test(`retry refuses mismatching or malformed environment ${JSON.stringify(changes)}`, () => {
    const { journal } = setup();
    journal.begin(input());
    assert.equal(journal.requestForRetry(environment(changes)), null);
    assert.equal(journal.read().kind, 'pending');
    assert.equal(journal.begin(input()), null);
  });
}

for (const [label, change] of [
  ['other operation ID', value => { value.operation_id = OTHER; }],
  ['nil reception', value => { value.reception_id = NIL; }],
  ['noncanonical UUID', value => { value.database_id = DATABASE.toUpperCase(); }],
  ['other action', value => { value.request.action = 'seat'; }],
  ['string revision', value => { value.request.expected_revision = '4'; }],
  ['negative revision', value => { value.request.expected_revision = -1; }],
  ['unsafe revision', value => { value.request.expected_revision = Number.MAX_SAFE_INTEGER + 1; }],
  ['infinite revision', value => { value.request.expected_revision = Infinity; }],
  ['token field', value => { value.staff_token = 'MUST-NOT-BE-STORED'; }],
  ['CSRF field', value => { value.request.csrf_token = 'MUST-NOT-BE-STORED'; }],
  ['customer fields', value => { value.adults = 2; }],
]) {
  test(`invalid begin refuses ${label} without persisting its content`, () => {
    const { journal, storage } = setup();
    const value = input(); change(value);
    assert.equal(journal.begin(value), null);
    assert.equal(storage.getItem(OPERATION_JOURNAL_KEY), null);
    assert.deepEqual(journal.read(), { kind: 'empty' });
  });
}

test('24 hours is an exact upper bound; old body and epoch are discarded', () => {
  const { journal, storage, setTime, reload } = setup();
  journal.begin(input());
  setTime(START + OPERATION_RECOVERY_MS - 1);
  assert.equal(journal.read().kind, 'pending');
  setTime(START + OPERATION_RECOVERY_MS);
  const manual = journal.read();
  assert.deepEqual(manual, { kind: 'manual', source: 'v2', record: { version: 2, state: 'manual', ...proof(), action: 'call', reason: 'expired' } });
  const raw = storage.getItem(OPERATION_JOURNAL_KEY);
  assert.equal(raw.includes('request'), false);
  assert.equal(raw.includes(EPOCH), false);
  assert.equal(raw.includes('expected_revision'), false);
  assert.equal(journal.requestForRetry(environment()), null);
  assert.equal(journal.begin(input()), null);
  setTime(START + OPERATION_RECOVERY_MS * 30);
  assert.deepEqual(reload().read(), manual);
});

test('backward clock change also leaves a manual marker with no replayable body', () => {
  const { journal, setTime, reload } = setup();
  journal.begin(input());
  setTime(START - 1);
  assert.equal(journal.read().record.reason, 'clock_reversed');
  assert.equal(journal.requestForRetry(environment()), null);
  assert.equal(reload().read().kind, 'manual');
});

test('invalid clock never permits a new operation or a retry', () => {
  const storage = new MemoryStorage();
  const journal = createOperationJournal({ storage, now: () => NaN });
  assert.equal(journal.begin(input()), null);
  assert.equal(journal.read().kind, 'blocked');
  assert.equal(journal.requestForRetry(environment()), null);
});

test('overlarge time and throwing clock fail closed', () => {
  for (const now of [() => Number.MAX_SAFE_INTEGER, () => { throw Error('private clock value'); }]) {
    const journal = createOperationJournal({ storage: new MemoryStorage(), now });
    assert.equal(journal.begin(input()), null);
    assert.equal(journal.read().kind, 'blocked');
  }
});

test('only exact matching receipt and current-context identity may resolve', () => {
  const { journal, reload } = setup();
  journal.begin(input());
  for (const wrong of [{ operation_id: OTHER }, { reception_id: OTHER }, { database_id: OTHER }, { extra: true }]) {
    assert.equal(journal.resolve(proof(wrong)), false);
    assert.equal(journal.read().kind, 'pending');
  }
  assert.equal(journal.resolve(proof()), true);
  assert.deepEqual(journal.read(), { kind: 'empty' });
  assert.deepEqual(reload().read(), { kind: 'empty' });
  assert.equal(journal.resolve(proof()), false);
  assert.ok(journal.begin(input({ operation_id: OTHER,
    request: { ...input().request, operation_id: OTHER } })));
});

test('manual markers remain blocked until caller matches a definitive result', () => {
  const { journal, setTime } = setup();
  journal.begin(input());
  setTime(START + OPERATION_RECOVERY_MS);
  assert.equal(journal.read().kind, 'manual');
  assert.equal(journal.resolve(proof({ database_id: OTHER })), false);
  assert.equal(journal.resolve(proof()), true);
  assert.equal(journal.read().kind, 'empty');
});

test('unavailable storage, read failure and disabled storage never permit a send', () => {
  for (const storage of [undefined, null, {}, { getItem() { throw Error('private storage failure'); } }]) {
    const journal = createOperationJournal({ storage, now: () => START });
    assert.equal(journal.begin(input()), null);
    const state = journal.read();
    assert.equal(state.kind, 'blocked');
    assert.equal(JSON.stringify(state).includes('private'), false);
  }
});

test('failure to write the guard or request prevents sending', () => {
  for (const failedKey of [OPERATION_JOURNAL_GUARD_KEY, OPERATION_JOURNAL_KEY]) {
    const { journal, storage, reload } = setup();
    journal.read();
    const set = storage.setItem.bind(storage);
    storage.setItem = (key, value) => {
      if (key === failedKey) throw Error('quota');
      set(key, value);
    };
    assert.equal(journal.begin(input()), null);
    assert.equal(journal.read().kind, 'blocked');
    if (failedKey === OPERATION_JOURNAL_KEY) assert.equal(reload().read().kind, 'blocked');
  }
});

test('silently discarded or modified writes do not pass the read-back check', () => {
  for (const mutate of [value => null, value => `${value} `]) {
    const { journal, storage } = setup();
    journal.read();
    const set = storage.setItem.bind(storage);
    storage.setItem = (key, value) => {
      const modified = key === OPERATION_JOURNAL_KEY ? mutate(value) : value;
      if (modified !== null) set(key, modified);
    };
    assert.equal(journal.begin(input()), null);
    assert.equal(journal.read().kind, 'blocked');
  }
});

test('known pending payload deletion or guard deletion is detected on reload', () => {
  for (const key of [OPERATION_JOURNAL_KEY, OPERATION_JOURNAL_GUARD_KEY]) {
    const { journal, storage, reload } = setup();
    journal.begin(input());
    storage.removeItem(key);
    assert.equal(reload().read().kind, 'blocked');
    assert.equal(journal.requestForRetry(environment()), null);
  }
});

test('complete storage deletion is detected while this journal is alive', () => {
  const { journal, storage } = setup();
  journal.begin(input());
  storage.clear();
  assert.equal(journal.read().kind, 'blocked');
  assert.equal(journal.begin(input()), null);
});

test('the same instance detects replacing pending contents with a fake empty guard', () => {
  const { journal, storage } = setup();
  journal.begin(input());
  storage.removeItem(OPERATION_JOURNAL_KEY);
  storage.setItem(OPERATION_JOURNAL_GUARD_KEY, 'empty');
  assert.equal(journal.read().kind, 'blocked');
});

test('no journal can infer vanished history after complete storage loss and reload', () => {
  // This documents the boundary rather than claiming all-case recovery. Web
  // Locks and a server operation ledger complement, but do not replace, it.
  const { journal, storage, reload } = setup();
  journal.begin(input());
  storage.clear();
  assert.equal(reload().read().kind, 'empty');
});

for (const [label, mutate] of [
  ['invalid JSON', () => '{'],
  ['duplicate JSON keys', raw => JSON.stringify(raw).replace('"version":2', '"version":99,"version":2')],
  ['null', () => 'null'],
  ['array', () => '[]'],
  ['unknown version', raw => { raw.version = 99; return JSON.stringify(raw); }],
  ['unknown state', raw => { raw.state = 'success'; return JSON.stringify(raw); }],
  ['unexpected customer content', raw => { raw.prediction = { minutes: 20 }; return JSON.stringify(raw); }],
  ['mismatched operation', raw => { raw.request.operation_id = OTHER; return JSON.stringify(raw); }],
  ['wrong expiration', raw => { raw.expires_at += 1; return JSON.stringify(raw); }],
  ['invalid revision', raw => { raw.request.expected_revision = 4.5; return JSON.stringify(raw); }],
  ['too large', () => 'x'.repeat(4097)],
]) {
  test(`corrupted journal is never used: ${label}`, () => {
    const { journal, storage, reload } = setup();
    journal.begin(input());
    const raw = JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY));
    storage.setItem(OPERATION_JOURNAL_KEY, mutate(raw));
    const recovered = reload();
    assert.equal(recovered.read().kind, 'blocked');
    assert.equal(recovered.requestForRetry(environment()), null);
    assert.equal(recovered.resolve(proof()), false);
    assert.equal(recovered.begin(input()), null);
  });
}

test('a changed operation in this tab cannot silently replace the prior journal', () => {
  const { journal, storage } = setup();
  journal.begin(input());
  const raw = JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY));
  raw.operation_id = OTHER; raw.request.operation_id = OTHER;
  storage.setItem(OPERATION_JOURNAL_KEY, JSON.stringify(raw));
  storage.setItem(OPERATION_JOURNAL_GUARD_KEY, `pending:${OTHER}`);
  assert.equal(journal.read().kind, 'blocked');
});

for (const [label, mutate] of [
  ['revision', record => { record.request.expected_revision += 1; }],
  ['epoch', record => { record.request.write_epoch = OTHER; }],
  ['creation and expiration', record => { record.created_at += 100; record.expires_at += 100; }],
  ['manual state', record => ({ version: 2, state: 'manual', ...proof(), action: 'call', reason: 'expired' })],
]) {
  test(`known fixed full journal cannot change its ${label}`, () => {
    const { journal, storage } = setup();
    journal.begin(input());
    const original = JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY));
    const altered = mutate(original) ?? original;
    storage.setItem(OPERATION_JOURNAL_KEY, JSON.stringify(altered));
    storage.setItem(OPERATION_JOURNAL_GUARD_KEY, `${altered.state}:${altered.operation_id}`);
    assert.deepEqual(journal.read(), { kind: 'blocked', reason: 'changed_journal' });
    assert.equal(journal.requestForRetry(environment()), null);
    assert.equal(journal.resolve(proof()), false);
    assert.equal(journal.begin(input()), null);
  });
}

test('a restored fixed journal also remembers its full original content', () => {
  const { journal, storage, reload } = setup();
  journal.begin(input());
  const recovered = reload();
  assert.equal(recovered.read().record.version, 2);
  const altered = JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY));
  altered.request.expected_revision += 1;
  storage.setItem(OPERATION_JOURNAL_KEY, JSON.stringify(altered));
  assert.deepEqual(recovered.read(), { kind: 'blocked', reason: 'changed_journal' });
});

test('failure while shrinking an expired journal prevents resend and new IDs', () => {
  const { journal, storage, setTime } = setup();
  journal.begin(input());
  setTime(START + OPERATION_RECOVERY_MS);
  storage.setItem = () => { throw Error('storage quota'); };
  assert.equal(journal.read().kind, 'blocked');
  assert.equal(journal.requestForRetry(environment()), null);
  assert.equal(journal.begin(input()), null);
});

test('interrupted resolution stays closed and cannot be misread as an empty slot', () => {
  const { journal, storage, reload } = setup();
  journal.begin(input());
  storage.setItem = () => { throw Error('storage unavailable'); };
  assert.equal(journal.resolve(proof()), false);
  assert.equal(journal.read().kind, 'blocked');
  assert.equal(reload().read().kind, 'blocked');
});

test('logout, request failure, a 404 or polling has no journal-clearing API', () => {
  const { journal, reload } = setup();
  journal.begin(input());
  assert.deepEqual(Object.keys(journal).sort(), ['begin', 'read', 'requestForRetry', 'resolve']);
  for (let index = 0; index < 30; index += 1) assert.equal(journal.read().kind, 'pending');
  assert.equal(reload().read().kind, 'pending');
});

// Compatibility: v1 is always read in place and remains call-only.
import { createCallJournal, CALL_JOURNAL_KEY, CALL_JOURNAL_GUARD_KEY } from '../../web/staff/assets/staff-call-journal.mjs';

for (const action of ['call', 'absent']) {
  test(`${action} occupies the one slot and prevents either subsequent action`, () => {
    const { journal, reload } = setup();
    const original = input({ request: { ...input().request, action } });
    const record = journal.begin(original);
    assert.equal(record.version, 2);
    assert.equal(record.action, action);
    for (const other of ['call', 'absent']) {
      assert.equal(reload().begin(input({ request: { ...input().request, action: other } })), null);
    }
    assert.equal(reload().read().record.action, action);
  });
  test(`expired ${action} retains identity and action without body or epoch`, () => {
    const { journal, reload, setTime, storage } = setup();
    journal.begin(input({ request: { ...input().request, action } }));
    setTime(START + OPERATION_RECOVERY_MS);
    assert.equal(reload().read().record.action, action);
    const raw = storage.getItem(OPERATION_JOURNAL_KEY);
    assert.equal(raw.includes('request'), false);
    assert.equal(raw.includes(EPOCH), false);
    assert.equal(reload().begin(input()), null);
  });
}

for (const manual of [false, true]) {
  test(`legacy ${manual ? 'manual' : 'pending'} blocks both actions and resolves before v2 initialization`, () => {
    const { storage, journal, setTime } = setup();
    const old = createCallJournal({ storage, now: () => START });
    const saved = old.begin(input());
    if (manual) setTime(START + OPERATION_RECOVERY_MS);
    const found = journal.read();
    assert.equal(found.source, 'legacy');
    assert.equal(found.record.version, 1);
    assert.equal(found.record.action, 'call');
    assert.equal(found.kind, manual ? 'manual' : 'pending');
    if (!manual) assert.equal(storage.getItem(CALL_JOURNAL_KEY), JSON.stringify(saved));
    assert.equal(storage.getItem(OPERATION_JOURNAL_GUARD_KEY), null);
    assert.equal(journal.begin(input()), null);
    assert.equal(journal.requestForRetry(environment()), null);
    assert.equal(journal.resolve(proof({ operation_id: OTHER })), false);
    assert.equal(journal.resolve(proof()), true);
    assert.equal(storage.getItem(CALL_JOURNAL_GUARD_KEY), 'empty');
    assert.equal(storage.getItem(CALL_JOURNAL_KEY), null);
    assert.ok(journal.begin(input({ request: { ...input().request, action: 'absent' } })));
  });
}

test('legacy and v2 unresolved records, even identical IDs, never merge', () => {
  const { storage, journal } = setup();
  journal.begin(input());
  createCallJournal({ storage, now: () => START }).begin(input());
  const before = new Map(storage.values);
  assert.equal(journal.read().kind, 'blocked');
  assert.equal(journal.resolve(proof()), false);
  assert.deepEqual(storage.values, before);
});

for (const legacy of [true, false]) {
  for (const guard of [false, true]) {
    test(`orphaned ${legacy ? 'v1' : 'v2'} ${guard ? 'guard' : 'record'} fails closed`, () => {
      const { storage, journal } = setup();
      const creator = legacy ? createCallJournal({ storage, now: () => START }) : createOperationJournal({ storage, now: () => START });
      creator.begin(input());
      storage.removeItem(legacy ? guard ? CALL_JOURNAL_KEY : CALL_JOURNAL_GUARD_KEY
        : guard ? OPERATION_JOURNAL_KEY : OPERATION_JOURNAL_GUARD_KEY);
      assert.equal(journal.read().kind, 'blocked');
      assert.equal(journal.begin(input()), null);
    });
  }
}

test('observed legacy cannot vanish into a new empty slot', () => {
  const { storage, journal } = setup();
  createCallJournal({ storage, now: () => START }).begin(input());
  assert.equal(journal.read().source, 'legacy');
  storage.clear();
  assert.equal(journal.begin(input()), null);
  assert.equal(journal.read().kind, 'blocked');
});

test('changing action or inserting authentication into an existing v2 request is rejected', () => {
  for (const mutate of [r => { r.request.action = 'absent'; }, r => { r.action = 'absent'; },
    r => { r.action = 'absent'; r.request.action = 'absent'; }, r => { r.request.csrf = 'synthetic-only'; }]) {
    const { journal, storage } = setup();
    journal.begin(input());
    const record = JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY)); mutate(record);
    storage.setItem(OPERATION_JOURNAL_KEY, JSON.stringify(record));
    assert.equal(journal.read().kind, 'blocked');
    assert.equal(journal.requestForRetry(environment()), null);
  }
});

test('unknown or corrupted legacy data never initializes v2', () => {
  for (const raw of ['{', 'null', JSON.stringify({ version: 99 }), 'x'.repeat(4097)]) {
    const { journal, storage } = setup();
    storage.setItem(CALL_JOURNAL_KEY, raw);
    storage.setItem(CALL_JOURNAL_GUARD_KEY, `pending:${OP}`);
    assert.equal(journal.read().kind, 'blocked');
    assert.equal(storage.getItem(OPERATION_JOURNAL_GUARD_KEY), null);
  }
});
