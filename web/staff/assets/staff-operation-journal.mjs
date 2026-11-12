// Inert foundation: no browser storage is accessed on import.
// No authentication, reception details or historical receipts are persisted.
import { createCallJournal, CALL_JOURNAL_KEY as LEGACY_KEY,
  CALL_JOURNAL_GUARD_KEY as LEGACY_GUARD } from './staff-call-journal.mjs';
export const OPERATION_JOURNAL_KEY = 'omachi.staff.operation.pending.v2';
export const OPERATION_JOURNAL_GUARD_KEY = 'omachi.staff.operation.guard.v2';
export const OPERATION_RECOVERY_MS = 24 * 60 * 60 * 1000;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const NIL = '00000000-0000-0000-0000-000000000000';
const uuid = value => typeof value === 'string' && UUID.test(value) && value !== NIL;
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  && Object.getPrototypeOf(value) === Object.prototype;
const keys = (value, expected) => plain(value) && Object.keys(value).length === expected.length
  && expected.every(key => Object.hasOwn(value, key));
const safeTime = value => Number.isSafeInteger(value) && value >= 0;
const copy = value => JSON.parse(JSON.stringify(value));
const freeze = value => {
  if (value && typeof value === 'object') {
    for (const item of Object.values(value)) freeze(item);
    Object.freeze(value);
  }
  return value;
};
const identityKeys = ['operation_id', 'reception_id', 'database_id'];
const validIdentity = value => identityKeys.every(key => uuid(value[key]));
const requestKeys = ['operation_id', 'action', 'expected_revision', 'write_epoch'];
const validRequest = value => keys(value, requestKeys) && uuid(value.operation_id)
  && ['call', 'absent'].includes(value.action) && Number.isSafeInteger(value.expected_revision)
  && value.expected_revision >= 0 && uuid(value.write_epoch);
const pendingKeys = ['version', 'state', ...identityKeys, 'action', 'request', 'created_at', 'expires_at'];
const manualKeys = ['version', 'state', ...identityKeys, 'action', 'reason'];
const manualReasons = new Set(['expired', 'clock_reversed']);
const validRecord = value => {
  if (!plain(value) || value.version !== 2 || !validIdentity(value) || !['call', 'absent'].includes(value.action)) return false;
  if (value.state === 'manual') return keys(value, manualKeys) && manualReasons.has(value.reason);
  return value.state === 'pending' && keys(value, pendingKeys) && validRequest(value.request)
    && value.request.operation_id === value.operation_id && value.request.action === value.action && safeTime(value.created_at)
    && safeTime(value.expires_at) && value.expires_at - value.created_at === OPERATION_RECOVERY_MS;
};
const identity = record => Object.fromEntries(identityKeys.map(key => [key, record[key]]));
const guardFor = record => `${record.state}:${record.operation_id}`;

// read() -> {kind: empty|pending|manual|blocked, record?, reason?}
// begin() -> immutable pending record, or null (no POST is allowed on null).
// requestForRetry() -> immutable fixed request, or null. This never sends.
// resolve() -> boolean. The caller must first match a definitive saved receipt
// to this identity AND read current context; a 404/timeout is not resolution.
function createV2Journal({ storage, now = Date.now } = {}) {
  let blockedReason = null;
  let observed = false;
  let knownRecord = null;
  const block = reason => { blockedReason = reason; return { kind: 'blocked', reason }; };
  const blocked = () => ({ kind: 'blocked', reason: blockedReason });
  const clock = () => {
    try { const value = now(); return safeTime(value) ? value : null; } catch { return null; }
  };
  const readStored = () => {
    const guard = storage.getItem(OPERATION_JOURNAL_GUARD_KEY);
    const raw = storage.getItem(OPERATION_JOURNAL_KEY);
    if ((guard !== null && typeof guard !== 'string') || (raw !== null && typeof raw !== 'string')) {
      throw new Error('invalid storage');
    }
    return { guard, raw };
  };
  const writeRecord = record => {
    // Write the guard first: interruption cannot turn an unresolved request
    // into an apparently empty slot. All intermediate forms fail closed.
    const serialized = JSON.stringify(record);
    const guard = guardFor(record);
    storage.setItem(OPERATION_JOURNAL_GUARD_KEY, guard);
    storage.setItem(OPERATION_JOURNAL_KEY, serialized);
    const saved = readStored();
    if (saved.guard !== guard || saved.raw !== serialized) throw new Error('storage verification failed');
    knownRecord = serialized;
    return freeze(copy(record));
  };
  const read = () => {
    if (blockedReason) return blocked();
    try {
      const { guard, raw } = readStored();
      if (guard === null && raw === null) {
        if (observed) return block('missing_journal');
        storage.setItem(OPERATION_JOURNAL_GUARD_KEY, 'empty');
        const check = readStored();
        if (check.guard !== 'empty' || check.raw !== null) return block('storage_verification');
        observed = true;
        return { kind: 'empty' };
      }
      observed = true;
      if (guard === 'empty' && raw === null) {
        return knownRecord ? block('missing_journal') : { kind: 'empty' };
      }
      if (raw === null || guard === null) return block('missing_journal');
      // Bound parsing of a browser-controlled journal. Our own records are
      // smaller than 1 KiB; no response, row or authentication data belongs here.
      if (raw.length > 4096) return block('invalid_journal');
      const record = JSON.parse(raw);
      if (!validRecord(record) || JSON.stringify(record) !== raw || guard !== guardFor(record)) {
        return block('invalid_journal');
      }
      // Do not accept a different revision, epoch, lifetime or state for a
      // request this instance has already fixed. Storage is not authoritative.
      if (knownRecord && knownRecord !== raw) {
        return block('changed_journal');
      }
      knownRecord = raw;
      if (record.state === 'manual') return { kind: 'manual', record: freeze(copy(record)) };
      const time = clock();
      if (time === null) return block('invalid_clock');
      if (time < record.created_at || time >= record.expires_at) {
        const marker = { version: 2, state: 'manual', ...identity(record), action: record.action,
          reason: time < record.created_at ? 'clock_reversed' : 'expired' };
        return { kind: 'manual', record: writeRecord(marker) };
      }
      return { kind: 'pending', record: freeze(copy(record)) };
    } catch { return block('storage_unavailable_or_invalid'); }
  };
  const begin = input => {
    const current = read();
    if (current.kind !== 'empty') return null;
    if (!keys(input, [...identityKeys, 'request']) || !validIdentity(input)
        || !validRequest(input.request) || input.operation_id !== input.request.operation_id) return null;
    const time = clock();
    if (time === null || !safeTime(time + OPERATION_RECOVERY_MS)) { block('invalid_clock'); return null; }
    // Canonical field order also makes the JSON body identical after reload.
    const request = Object.fromEntries(requestKeys.map(key => [key, input.request[key]]));
    const record = { version: 2, state: 'pending', ...identity(input), action: request.action, request,
      created_at: time, expires_at: time + OPERATION_RECOVERY_MS };
    try {
      const saved = writeRecord(record);
      return saved;
    } catch { block('storage_write_failed'); return null; }
  };
  const requestForRetry = environment => {
    const current = read();
    if (current.kind !== 'pending' || !keys(environment, ['database_id', 'write_epoch'])
        || !uuid(environment.database_id) || !uuid(environment.write_epoch)
        || environment.database_id !== current.record.database_id
        || environment.write_epoch !== current.record.request.write_epoch) return null;
    return current.record.request;
  };
  const resolve = expected => {
    const current = read();
    if (!['pending', 'manual'].includes(current.kind) || !keys(expected, identityKeys)
        || !validIdentity(expected)
        || identityKeys.some(key => current.record[key] !== expected[key])) return false;
    try {
      // If deletion or the final guard update fails, future reads block instead
      // of making a new operation. Only a proven result reaches this path.
      storage.removeItem(OPERATION_JOURNAL_KEY);
      storage.setItem(OPERATION_JOURNAL_GUARD_KEY, 'empty');
      const check = readStored();
      if (check.raw !== null || check.guard !== 'empty') throw new Error('storage verification failed');
      knownRecord = null;
      return true;
    } catch { block('storage_resolution_failed'); return false; }
  };
  return Object.freeze({ read, begin, requestForRetry, resolve });
}

// V1 is recovered in place as call. Never convert or delete an unresolved v1
// record. A verified resolution is the only transition to an empty v2 slot.
export function createOperationJournal(options = {}) {
  const modern = createV2Journal(options), legacy = createCallJournal(options);
  let blockedReason = null, legacySeen = false, modernSeen = false;
  const block = reason => { blockedReason = reason; return { kind: 'blocked', reason }; };
  const read = () => {
    if (blockedReason) return { kind: 'blocked', reason: blockedReason };
    try {
      const storage = options.storage;
      const oldRaw = storage.getItem(LEGACY_KEY), oldGuard = storage.getItem(LEGACY_GUARD);
      const newRaw = storage.getItem(OPERATION_JOURNAL_KEY), newGuard = storage.getItem(OPERATION_JOURNAL_GUARD_KEY);
      const oldPresent = oldRaw !== null || oldGuard !== null && oldGuard !== 'empty';
      const newPresent = newRaw !== null || newGuard !== null && newGuard !== 'empty';
      if (oldPresent && newPresent) return block('conflicting_journals');
      if (modernSeen && oldPresent && modern.read().kind !== 'empty') return block('changed_journal');
      // Also inspect a legacy empty guard, to detect changes observed by v1.
      if (legacySeen || oldRaw !== null || oldGuard !== null) {
        legacySeen = true;
        const prior = legacy.read();
        if (prior.kind === 'blocked') return block('invalid_legacy_journal');
        if (prior.kind !== 'empty') {
          // Inspect an existing modern empty guard without initializing v2.
          if (newGuard !== null && modern.read().kind !== 'empty') return block('invalid_journal');
          return { ...prior, source: 'legacy', record: freeze({ ...prior.record, action: 'call' }) };
        }
      }
      modernSeen = true;
      const current = modern.read();
      return current.record ? { ...current, source: 'v2' } : current;
    } catch { return block('storage_unavailable_or_invalid'); }
  };
  const begin = input => read().kind === 'empty' ? modern.begin(input) : null;
  const requestForRetry = environment => {
    const current = read();
    // Legacy compatibility is deliberately query-only. Its original fixed
    // request stays intact until a definitive receipt and context resolve it.
    return current.source === 'v2' ? modern.requestForRetry(environment) : null;
  };
  const resolve = expected => {
    const current = read();
    if (!['pending', 'manual'].includes(current.kind)) return false;
    const resolved = current.source === 'legacy' ? legacy.resolve(expected) : modern.resolve(expected);
    if (!resolved) return false;
    return read().kind === 'empty';
  };
  return Object.freeze({ read, begin, requestForRetry, resolve });
}
