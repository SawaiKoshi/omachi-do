// Foundation only. Nothing imports or instantiates this from the browser UI.
import { createOperationJournal } from './staff-operation-journal.mjs';
import { operationLabResponse, validOperationContext, validOperationReceipt,
  canApplyOperation, contractSignature, receiptMatchesCurrent } from './staff-operation-contract.mjs';

// Keep the v1 name so cooperating old and new clients share one origin lock.
export const OPERATION_LOCK_NAME = 'omachi.synthetic.staff.call.v1';
const immutable = value => {
  const cloned = JSON.parse(JSON.stringify(value));
  const freeze = item => { if (item && typeof item === 'object') { Object.values(item).forEach(freeze); Object.freeze(item); } return item; };
  return freeze(cloned);
};
const signature = context => contractSignature({ reception: context.reception,
  metadata: Object.fromEntries(Object.entries(context.metadata).filter(([k]) => k !== 'view_version')) });

export function createStaffOperations({ window: env, fetchImpl, security, now = Date.now, authFailure = () => {} }) {
  let journal;
  try { journal = createOperationJournal({ storage: env.sessionStorage, now }); }
  catch { journal = createOperationJournal({ now }); }
  let alive = true, busy = false, lockState = 'checking', releaseLock;
  let authExpired = false, responseGeneration = 0;
  let confirmation = null, confirmationContext = null, phase = 'idle', received = null, receivedVersion = null, resolved = null, lookupVersion = null;
  const secure = () => env.location?.protocol === 'https:' && env.location?.hostname === 'staff.localhost';
  const active = () => alive && !authExpired && secure() && security().active;
  const currentAuth = version => active() && security().version === version;
  const currentRequest = (version, generation) => currentAuth(version) && generation === responseGeneration;
  const writable = () => active() && security().ready && lockState === 'owned';
  const state = () => journal.read();
  const status = () => {
    const visible = active(), version = security().version;
    return immutable({ phase, busy, lockState, authExpired, journal: state(),
      receipt: visible ? receivedVersion === version && received || resolved?.authVersion === version && resolved.receipt || null : null,
      current: visible && resolved?.authVersion === version ? resolved.current : null });
  };
  function clearConfirmation() { confirmation = null; confirmationContext = null; }
  function clearDisplay() {
    clearConfirmation(); received = null; receivedVersion = null; resolved = null; lookupVersion = null;
  }
  function expireAuthentication() {
    if (!alive || authExpired) return;
    // Irreversible for this instance. Close access BEFORE notifying a caller;
    // never resolve, delete, or rewrite its uncertain operation here.
    authExpired = true; responseGeneration++; clearDisplay(); phase = 'auth_expired';
    try { Promise.resolve(authFailure()).catch(() => {}); } catch { /* Notification cannot reopen access. */ }
  }
  const unknown = error => {
    if (authExpired) return false;
    if (error.message === 'auth') { expireAuthentication(); return false; }
    phase = received ? 'current_unverified' : 'unknown';
    return false;
  };

  // Each write/read owns its controller. List polling cannot cancel it.
  async function request(path, method = 'GET', body) {
    if (!active()) throw Error('inactive');
    const version = security().version, generation = responseGeneration;
    const controller = new env.AbortController();
    let timer;
    try {
      const timeout = new Promise((_, reject) => {
        timer = env.setTimeout(() => { controller.abort(); reject(Error('unknown')); }, 15_000);
      });
      const headers = { Accept: 'application/json' };
      if (method === 'POST') {
        if (!writable() || !security().csrf) { expireAuthentication(); throw Error('auth'); }
        headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = security().csrf;
      }
      const response = await Promise.race([fetchImpl(path, { method, headers,
        ...(body === undefined ? {} : { body }), signal: controller.signal, credentials: 'same-origin',
        mode: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' }), timeout]);
      if (!currentRequest(version, generation)) throw Error('stale');
      // A 401/403 closes access even when an intermediary omits lab headers.
      if ([401, 403].includes(response.status)) { expireAuthentication(); throw Error('auth'); }
      if (!operationLabResponse(response)) throw Error('environment');
      const value = await Promise.race([response.json(), timeout]);
      if (!currentRequest(version, generation)) throw Error('stale');
      return { status: response.status, value };
    } finally { env.clearTimeout(timer); }
  }
  async function context(id) {
    const response = await request(`/staff/receptions/${encodeURIComponent(id)}/operation-context`);
    if (response.status !== 200 || !validOperationContext(response.value, id)) throw Error('context');
    return response.value;
  }
  async function settle(receipt, record, version) {
    if (!currentAuth(version)) return false;
    // Memory only: this is a historical receipt, not a replacement list row.
    received = immutable(receipt); receivedVersion = version; phase = 'current_unverified';
    const current = await context(record.reception_id);
    if (!currentAuth(version)) return false;
    if (current.metadata.database_id !== record.database_id
        || record.request && current.metadata.write_epoch !== record.request.write_epoch
        || !receiptMatchesCurrent(receipt, current.reception)) throw Error('environment');
    if (!journal.resolve({ operation_id: record.operation_id, reception_id: record.reception_id, database_id: record.database_id })) throw Error('storage');
    resolved = immutable({ receipt, current, authVersion: version }); received = null; receivedVersion = null;
    phase = receipt.outcome === 'rejected' ? 'rejected' : receipt.outcome === 'no_change' ? 'no_change' : 'resolved';
    return true;
  }
  async function transmit(record, version, before = null) {
    const fixed = journal.requestForRetry({ database_id: record.database_id, write_epoch: record.request.write_epoch });
    if (!writable() || !currentAuth(version) || !fixed || contractSignature(fixed) !== contractSignature(record.request)) throw Error('fixed_request');
    phase = 'sending';
    const response = await request(`/staff/receptions/${record.reception_id}/operations`, 'POST', JSON.stringify(fixed));
    if (!currentAuth(version)) return false;
    if (response.status !== response.value?.response_status || !validOperationReceipt(response.value, record, before)) throw Error('unknown');
    return settle(response.value, record, version);
  }
  async function prepare(action, id) {
    if (!writable() || busy || state().kind !== 'empty' || !['call', 'absent'].includes(action)) return null;
    busy = true; clearConfirmation(); const version = security().version;
    try {
      const latest = await context(id);
      if (!currentAuth(version) || !writable() || state().kind !== 'empty' || !canApplyOperation(action, latest.reception)) return null;
      const generation = responseGeneration;
      // Retaining a ticket must not retain an accessible confirmation after
      // expiry/stop. A caller's already-copied data still belongs to its UI.
      const ticket = Object.freeze({ action, authVersion: version,
        get context() { return currentRequest(version, generation) && confirmation === ticket ? confirmationContext : null; } });
      confirmationContext = immutable(latest); confirmation = ticket;
      return ticket;
    } catch (error) { if (currentAuth(version)) unknown(error); return null; }
    finally { busy = false; }
  }
  async function confirm(ticket) {
    if (!ticket || ticket !== confirmation || !writable() || busy || state().kind !== 'empty'
        || ticket.authVersion !== security().version) return false;
    const selected = confirmationContext;
    busy = true; clearConfirmation(); const version = security().version;
    try {
      const latest = await context(selected.reception.id);
      if (!currentAuth(version) || !writable() || signature(latest) !== signature(selected)
          || !canApplyOperation(ticket.action, latest.reception)) return false;
      const operation_id = env.crypto.randomUUID();
      const record = journal.begin({ operation_id, reception_id: latest.reception.id, database_id: latest.metadata.database_id,
        request: { operation_id, action: ticket.action, expected_revision: latest.reception.revision, write_epoch: latest.metadata.write_epoch } });
      if (!record) { phase = 'blocked'; return false; }
      received = null; resolved = null; lookupVersion = version;
      return await transmit(record, version, latest.reception);
    } catch (error) { return currentAuth(version) ? unknown(error) : false; }
    finally { busy = false; }
  }
  async function lookup() {
    const pending = state();
    if (!active() || busy || !['pending', 'manual'].includes(pending.kind)) return false;
    busy = true; clearConfirmation(); const version = security().version, record = pending.record;
    phase = 'checking'; lookupVersion = null;
    try {
      const binding = await context(record.reception_id);
      if (!currentAuth(version) || binding.metadata.database_id !== record.database_id) throw Error('environment');
      const response = await request(`/staff/operations/${record.operation_id}`);
      if (!currentAuth(version)) return false;
      lookupVersion = version;
      if (response.status !== 200 || !validOperationReceipt(response.value, record)) throw Error('unknown');
      return await settle(response.value, record, version);
    } catch (error) { return currentAuth(version) ? unknown(error) : false; }
    finally { busy = false; }
  }
  async function retry() {
    const pending = state();
    // A new login must first query. A received receipt must never be reposted
    // just because the current-state read failed.
    if (!writable() || busy || received || pending.kind !== 'pending' || pending.source !== 'v2'
        || lookupVersion !== security().version) return false;
    busy = true; clearConfirmation(); const version = security().version;
    try {
      const latest = await context(pending.record.reception_id);
      if (!currentAuth(version) || !journal.requestForRetry({ database_id: latest.metadata.database_id, write_epoch: latest.metadata.write_epoch })) throw Error('environment');
      // Retry keeps the old revision even when current state has advanced.
      return await transmit(pending.record, version);
    } catch (error) { return currentAuth(version) ? unknown(error) : false; }
    finally { busy = false; }
  }
  try {
    if (!secure() || !env.navigator?.locks?.request) throw Error('locks');
    void env.navigator.locks.request(OPERATION_LOCK_NAME, { mode: 'exclusive', ifAvailable: true }, async lock => {
      if (!alive) return;
      lockState = lock ? 'owned' : 'other';
      if (lock) await new Promise(resolve => { releaseLock = resolve; });
    }).catch(() => { lockState = 'unavailable'; });
  } catch { lockState = 'unavailable'; }
  return Object.freeze({ prepare, confirm, lookup, retry, status, expireAuthentication,
    stop() { alive = false; responseGeneration++; clearDisplay(); phase = authExpired ? 'auth_expired' : 'stopped'; releaseLock?.(); },
  });
}
