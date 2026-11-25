import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';

import { createStaffApp } from '../../web/staff/assets/staff.mjs';
import { createCallJournal, CALL_JOURNAL_KEY, CALL_RECOVERY_MS } from '../../web/staff/assets/staff-call-journal.mjs';
import { validCallReceipt } from '../../web/staff/assets/staff-call.mjs';
import { createOperationJournal, OPERATION_JOURNAL_KEY, OPERATION_JOURNAL_GUARD_KEY, OPERATION_RECOVERY_MS } from '../../web/staff/assets/staff-operation-journal.mjs';

// The browser suite deliberately provides no real browser storage, cookies, DB,
// credentials, or network. All receipts and session responses are synthetic.
const IDS = [
  'login-view', 'login-form', 'staff-token', 'login-button', 'login-message',
  'staff-view', 'logout-button', 'refresh-button', 'status-message', 'updated-at',
  'waiting-count', 'called-count', 'active-list', 'recent-list',
  'absent-list', 'absent-empty', 'absent-tab', 'absent-panel',
  'active-tab-count', 'absent-tab-count', 'recent-tab-count', 'unseated-count',
  'detail-view', 'detail-title', 'detail-content', 'detail-close',
  'active-empty', 'recent-empty', 'security-note',
  'active-tab', 'recent-tab', 'active-panel', 'recent-panel', 'reception-scroll',
  'row-menu', 'row-menu-title', 'row-menu-status', 'row-menu-items', 'row-menu-close', 'synthetic-preview',
  'row-menu-note', 'staff-access-label', 'staff-list-help', 'staff-footer',
  'call-status-view', 'call-status-message', 'call-operation-id', 'call-lookup', 'call-retry',
  'update-status', 'call-success-view', 'call-success-message', 'call-success-close',
];

class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, handler, options = false) {
    const capture = typeof options === 'boolean' ? options : Boolean(options.capture);
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), { handler, capture }]);
  }
  removeEventListener(type, handler) {
    this.listeners.set(type, (this.listeners.get(type) ?? []).filter(item => item.handler !== handler));
  }
  dispatch(type, properties = {}) {
    const event = { type, target: this, detail: 0, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.stopped = true; },
      stopImmediatePropagation() { this.stopped = true; this.immediate = true; }, ...properties };
    const path = [this];
    let parent = this.parentElement ?? this.ownerDocument;
    while (parent && !path.includes(parent)) { path.push(parent); parent = parent.parentElement ?? parent.ownerDocument; }
    const results = [];
    const invoke = (target, capture) => {
      event.currentTarget = target;
      for (const item of target.listeners?.get(type) ?? []) {
        if (item.capture === capture) results.push(item.handler(event));
        if (event.immediate) break;
      }
    };
    for (const target of [...path].reverse()) { invoke(target, true); if (event.stopped) break; }
    if (!event.stopped) for (const target of path) { invoke(target, false); if (event.stopped) break; }
    return { event, results };
  }
  async emit(type, properties = {}) {
    const { event, results } = this.dispatch(type, properties);
    await Promise.all(results);
    await settle();
    return event;
  }
}

class Element extends Target {
  constructor(id = '') {
    super();
    this.id = id;
    this.value = '';
    this.hidden = ['staff-view', 'detail-view', 'row-menu', 'absent-panel', 'recent-panel', 'synthetic-preview'].includes(id);
    this.disabled = false;
    this.dataset = {};
    this.attributes = new Map();
    this.children = [];
    this._textContent = '';
    this.scrollTop = 0;
  }
  get textContent() {
    return this._textContent + this.children.map(child => child.textContent ?? child).join('');
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  set innerHTML(_value) { throw new Error('Staff/API text must never become HTML'); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  replaceChildren(...children) { this._textContent = ''; for (const child of this.children) child.parentElement = null; this.children = []; this.append(...children); }
  append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
  appendChild(child) { this.append(child); return child; }
  removeChild(child) {
    const index = this.children.indexOf(child);
    assert.notEqual(index, -1, 'removeChild requires an existing child');
    if (child.contains(this.ownerDocument?.activeElement)) this.ownerDocument.activeElement = null;
    this.children.splice(index, 1); child.parentElement = null;
    return child;
  }
  insertBefore(child, before) {
    if (child === before) return child;
    if (child.parentElement) child.parentElement.removeChild(child);
    const index = before === null ? this.children.length : this.children.indexOf(before);
    assert.notEqual(index, -1, 'insertBefore requires an existing reference child');
    child.parentElement = this; this.children.splice(index, 0, child);
    return child;
  }
  focus() { if (this.ownerDocument) { this.ownerDocument.activeElement = this; this.dispatch('focusin'); } }
  contains(target) { return target === this || this.children.some(child => child.contains?.(target)); }
  closest(selector) {
    if (selector === '[data-reception-id]' && this.dataset.receptionId) return this;
    return this.parentElement?.closest(selector) ?? null;
  }
}

const settle = async () => {
  for (let index = 0; index < 30; index += 1) await Promise.resolve();
};

const response = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  async json() { return body; },
});

const metadata = (overrides = {}) => ({
  schema_version: 2,
  database_id: 'de3a8c14-a6e9-436a-bdc1-69823522832c',
  write_epoch: '54adb75b-22f9-4104-85c2-1e7ed0f17602',
  staff_write_mode: 'revision_required',
  seat_corrections_enabled: false,
  write_enabled: true,
  disabled_reason: null,
  view_version: 'a'.repeat(64),
  ...overrides,
});

const reception = (index = 1, overrides = {}) => ({
  id: `8bcad442-85bd-4c4d-90a9-${String(index).padStart(12, '0')}`,
  display_number: `20261101-${String(index).padStart(3, '0')}`,
  reception_order: index,
  received_at: '2026-11-01T18:00:00+09:00',
  adults: 2, children: 1, total_people: 3,
  status: 'waiting', called_at: null, seated_at: null, cancelled_at: null,
  actual_wait_minutes: null, revision: 0,
  allowed_actions: ['call', 'seat', 'cancel'], valid_seating_event: null,
  ...overrides,
});

const session = (overrides = {}) => ({
  authenticated: true,
  csrf_token: 'c'.repeat(43),
  expires_in_seconds: 3600,
  idle_timeout_seconds: 900,
  principal: { principal_kind: 'shared_staff_token', principal_id: 'shared-staff' },
  ...overrides,
});

const listing = (rows = [], view = 'active', overrides = {}) => ({
  metadata: metadata(), view, as_of: '2026-11-01T18:30:00+09:00',
  receptions: rows, has_more: false, next_cursor: null,
  ...overrides,
});

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function harness({ fetch: fetchImpl, authenticated = true, rows = [], recent = [], now = 0, page = 'receptions',
  callLab = false, operationLab = false, seatLab = false, storage, lockAvailable = true, locksSupported = true, lockManager } = {}) {
  const elements = new Map(IDS.map(id => [id, new Element(id)]));
  const doc = Object.assign(new Target(), {
    visibilityState: 'visible', hidden: false, activeElement: null,
    body: { dataset: { staffPage: page } },
    getElementById(id) {
      assert.ok(elements.has(id), `Unexpected staff DOM id: ${id}`);
      return elements.get(id);
    },
    createElement(tag) { return Object.assign(new Element(tag), { ownerDocument: this, tagName: tag.toUpperCase() }); },
    querySelectorAll() { return []; },
  });
  for (const element of elements.values()) element.ownerDocument = doc;
  elements.get('call-success-view').hidden = true;
  elements.get('call-success-view').append(elements.get('call-success-message'), elements.get('call-success-close'));
  elements.get('update-status').append(elements.get('status-message'), elements.get('updated-at'), elements.get('call-success-view'));
  Object.defineProperty(doc, 'cookie', {
    get() { throw new Error('Staff UI must not inspect cookies'); },
    set() { throw new Error('Staff UI must not store shared credentials in cookies'); },
  });
  const requests = [];
  const redirects = [];
  const timers = new Map();
  let timerId = 0;
  let time = now;
  const env = Object.assign(new Target(), {
    AbortController,
    crypto: { randomUUID },
    location: { protocol: 'https:', hostname: 'staff.localhost', origin: 'https://staff.localhost', href: 'https://staff.localhost/staff/', replace(path) { redirects.push(path); } },
    now() { return time; },
    setTimeout(handler, delay) { timerId += 1; timers.set(timerId, { handler, at: time + delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  });
  if (callLab) Object.assign(env.location, { port: '8444', origin: 'https://staff.localhost:8444' });
  if (operationLab) {
    const port = seatLab ? '8446' : '8445', origin = `https://staff.localhost:${port}`;
    Object.assign(env.location, { port, origin, href: origin + '/staff/' });
  }
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(env, name, { get() {
      if ((callLab || operationLab) && name === 'sessionStorage' && storage) return storage;
      throw new Error(`Staff UI must not use ${name}`);
    } });
  }
  if (callLab || operationLab) env.navigator = locksSupported ? { locks: lockManager ?? { request: async (name, options, callback) => callback(lockAvailable ? { name } : null) } } : {};
  const fetcher = async (url, options = {}) => {
    requests.push({ url: String(url), ...options });
    if (fetchImpl) {
      const result = await fetchImpl(String(url), options);
      if (result !== undefined) return withCapability(result);
    }
    if (url === '/staff/session') {
      if ((options.method ?? 'GET') === 'DELETE') return withCapability(response({ authenticated: false }));
      return withCapability(authenticated ? response(session()) : response({ detail: 'Unauthorized' }, 401));
    }
    const parsed = new URL(String(url), env.location.origin);
    if (parsed.pathname === '/staff/receptions') {
      return withCapability(response(listing(parsed.searchParams.get('view') === 'recently_seated' ? recent : rows,
        parsed.searchParams.get('view') ?? 'active')));
    }
    const found = [...rows, ...recent].find(row => parsed.pathname === `/staff/receptions/${row.id}/operation-context`);
    if (found) return withCapability(response({ metadata: metadata(), reception: found }));
    assert.fail(`Unexpected staff request: ${options.method ?? 'GET'} ${url}`);
  };
  function withCapability(result) {
    if ((callLab || operationLab) && !result.headers) result.headers = { get: name => ({
      'X-Omachi-Preview': 'synthetic', 'X-Omachi-Preview-Actions': seatLab ? 'seat' : operationLab ? 'call,absent' : 'call',
    })[name] ?? null };
    return result;
  }
  env.fetch = fetcher;
  const controller = createStaffApp({ document: doc, fetchImpl: fetcher, window: env, now: () => time });
  return {
    controller, doc, env, requests, timers, redirects,
    el(id) { return elements.get(id); },
    async start() { await controller.start(); await settle(); },
    async tick(milliseconds) {
      const end = time + milliseconds;
      for (let attempt = 0; attempt < 1000; attempt += 1) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) { time = end; await settle(); return; }
        time = due[1].at;
        timers.delete(due[0]);
        due[1].handler();
        await settle();
      }
      assert.fail('Staff UI scheduled an unbounded timer loop');
    },
  };
}

function assertHidden(h) {
  assert.equal(h.el('staff-view').hidden, true);
  assert.equal(h.el('active-list').textContent, '');
  assert.equal(h.el('absent-list').textContent, '');
  assert.equal(h.el('recent-list').textContent, '');
  assert.equal(h.el('detail-view').hidden, true);
  assert.equal(h.el('detail-content').textContent, '');
}

function assertReadOnlyRequests(requests) {
  for (const request of requests) {
    const method = request.method ?? 'GET';
    assert.equal(request.credentials, 'same-origin');
    assert.equal(request.cache, 'no-store');
    assert.ok(method === 'GET' || (request.url === '/staff/session' && ['POST', 'DELETE'].includes(method)),
      `Unexpected state mutation: ${method} ${request.url}`);
    assert.doesNotMatch(request.url, /[?&](?:token|csrf|secret|password)=/i);
    const headerNames = Object.keys(request.headers ?? {}).map(name => name.toLowerCase());
    if (!(method === 'POST' && request.url === '/staff/session')) {
      assert.equal(headerNames.includes('x-staff-token'), false);
    }
  }
}

class CallStorage {
  constructor() { this.values = new Map(); this.fail = false; }
  getItem(key) { if (this.fail) throw new Error('storage unavailable'); return this.values.get(key) ?? null; }
  setItem(key, value) { if (this.fail) throw new Error('storage unavailable'); this.values.set(key, value); }
  removeItem(key) { if (this.fail) throw new Error('storage unavailable'); this.values.delete(key); }
}

function callReceipt(row, body, outcome = 'state_changed') {
  const receipt = Object.fromEntries(['id', 'display_number', 'reception_order', 'received_at', 'adults', 'children',
    'total_people', 'status', 'called_at', 'seated_at', 'cancelled_at', 'actual_wait_minutes'].map(key => [key, row[key]]));
  receipt.snapshot = { captured_at: row.received_at, source: 'synthetic', waiting_groups: 0, ahead_groups: 0,
    ahead_same_size_groups: 0, party_size_groups: { '1-2': 0, '3-4': 0, '5+': 0 } };
  receipt.prediction = { status: 'model_unavailable', minutes: null, model_version: null, predicted_at: row.received_at, warnings: [] };
  return { operation_id: body.operation_id, reception_id: row.id, action: 'call', outcome, response_status: 200,
    recorded_at: row.called_at, resulting_revision: body.expected_revision + (outcome === 'state_changed' ? 1 : 0),
    reception: receipt, error: null };
}

function callHarness(options = {}) {
  const rows = options.rows ?? [reception(1, options.row ?? {})];
  const storage = options.storage ?? new CallStorage();
  const ledger = options.ledger ?? new Map();
  let contexts = 0;
  const h = harness({ ...options, rows, callLab: options.callLab ?? true, storage, fetch: async (url, request) => {
    if (url.endsWith('/operation-context')) {
      contexts += 1;
      const row = rows.find(r => url === `/staff/receptions/${r.id}/operation-context`);
      const override = await options.context?.(row, contexts, url, request);
      if (override !== undefined) return override;
      return response({ metadata: metadata(options.meta), reception: { ...row } });
    }
    if (url.includes('/staff/operations/')) {
      const override = await options.lookup?.(url, request);
      if (override !== undefined) return override;
      const value = ledger.get(url.split('/').at(-1));
      return value ? response(value) : response({ detail: 'not found' }, 404);
    }
    if (url.endsWith('/operations') && request.method === 'POST') {
      const body = JSON.parse(request.body);
      assert.equal(body.action, 'call');
      assert.deepEqual(Object.keys(body), ['operation_id', 'action', 'expected_revision', 'write_epoch']);
      const saved = JSON.parse(storage.getItem(CALL_JOURNAL_KEY));
      assert.deepEqual(saved.request, body, 'Fixed recovery data must be saved before sending');
      assert.equal(request.headers['X-Staff-Token'], undefined);
      assert.equal(request.headers['X-CSRF-Token'], session().csrf_token);
      const overridden = await options.post?.(body, request, rows, ledger);
      if (overridden !== undefined) return overridden;
      if (ledger.has(body.operation_id)) return response(ledger.get(body.operation_id));
      const index = rows.findIndex(r => url === `/staff/receptions/${r.id}/operations`);
      rows[index] = { ...rows[index], status: 'called', revision: body.expected_revision + 1, called_at: '2026-11-01T18:10:00+09:00',
        allowed_actions: ['absent', 'seat', 'cancel'] };
      const result = callReceipt(rows[index], body);
      ledger.set(body.operation_id, result);
      return response(result);
    }
    return options.fetch?.(url, request);
  } });
  const postRequests = () => h.requests.filter(r => r.method === 'POST' && r.url.endsWith('/operations'));
  return { ...h, rows, storage, ledger, postRequests,
    async openCallMenu() {
      await h.el(rows[0].status === 'absent' ? 'absent-tab' : 'active-tab').emit('click');
      await rowTrigger(rowItem(h, 0, rows[0].status === 'absent' ? 'absent' : 'active')).emit('click');
      const button = h.el('row-menu-items').children.flatMap(item => item.children).find(item => item.tagName === 'BUTTON');
      assert.ok(button, 'Synthetic call menu must contain one direct action');
      this.actionButton = button;
    },
    async chooseCall() { await this.actionButton?.emit('click'); await settle(); await settle(); },
  };
}

for (const status of ['waiting', 'absent']) {
  test(`synthetic call ${status}->called validates two contexts and saves a fixed UUID before one POST`, async () => {
    const h = callHarness({ row: { status } }); await h.start();
    await h.openCallMenu();
    assert.equal(h.el('row-menu').hidden, false);
    assert.match(h.el('row-menu-title').textContent, /001番/);
    assert.equal(h.postRequests().length, 0);
    await h.chooseCall();
    assert.equal(h.postRequests().length, 1);
    assert.equal(h.rows[0].status, 'called');
    assert.equal(h.el('called-count').textContent, '1');
    assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null);
    assert.match(h.el('call-success-message').textContent, /呼出成功/);
    assert.ok(h.requests.filter(r => r.url.endsWith('/operation-context')).length >= 3);
    h.controller.stop();
  });
}

test('confirmed success uses the reserved slot and expires after five seconds without changing rows, menus, scroll or journal', async () => {
  const h = callHarness(); await h.start(); await h.openCallMenu(); await h.chooseCall();
  assert.equal(h.el('call-success-view').hidden, false);
  assert.equal(h.el('call-status-view').hidden, true);
  assert.equal(h.el('update-status').dataset.callNotice, 'true');
  const item = rowItem(h), guard = h.storage.getItem('omachi.staff.call.guard.v1');
  h.el('reception-scroll').scrollTop = 80;
  await rowTrigger(item).emit('click');
  const focus = h.doc.activeElement;
  await h.tick(4_999); assert.equal(h.el('call-success-view').hidden, false);
  await h.tick(1);
  assert.equal(h.el('call-success-view').hidden, true);
  assert.equal(h.el('update-status').dataset.callNotice, 'false');
  assert.equal(rowItem(h), item); assert.equal(h.el('called-count').textContent, '1');
  assert.equal(h.el('reception-scroll').scrollTop, 80);
  assert.equal(h.el('row-menu').hidden, false); assert.equal(h.doc.activeElement, focus);
  assert.equal(h.storage.getItem('omachi.staff.call.guard.v1'), guard);
  assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null); assert.equal(h.postRequests().length, 1);
  await h.controller.refresh(); assert.equal(h.el('call-success-view').hidden, true);
  h.controller.stop();
});

test('manual success dismissal only changes its display and returns keyboard focus to refresh', async () => {
  const h = callHarness(); await h.start(); await h.openCallMenu(); await h.chooseCall();
  const stored = [...h.storage.values], row = rowItem(h), requests = h.requests.length;
  h.el('call-success-close').focus();
  await h.el('call-success-close').emit('click');
  assert.equal(h.el('call-success-view').hidden, true);
  assert.deepEqual([...h.storage.values], stored); assert.equal(h.requests.length, requests);
  assert.equal(rowItem(h), row); assert.equal(h.doc.activeElement, h.el('refresh-button'));
  h.controller.stop();
});

test('a delayed successful list refresh starts the success lifetime only after all pages finish', async () => {
  const pending = deferred(); let delay = false;
  const h = callHarness({ post: () => { delay = true; }, fetch: url => {
    if (delay && url.startsWith('/staff/receptions?') && new URL(url, 'https://staff.localhost').searchParams.get('view') === 'active') return pending.promise;
  } });
  await h.start(); await h.openCallMenu();
  const sending = h.actionButton.emit('click'); await settle();
  await h.tick(5_000); assert.equal(h.el('call-success-view').hidden, true);
  delay = false; pending.resolve(response(listing(h.rows))); await sending; await settle(); await settle();
  assert.equal(h.el('call-success-view').hidden, false);
  await h.tick(4_999); assert.equal(h.el('call-success-view').hidden, false);
  await h.tick(1); assert.equal(h.el('call-success-view').hidden, true); h.controller.stop();
});

test('a successful receipt with failed latest-list refresh stays a persistent review message', async () => {
  let offline = false;
  const h = callHarness({ post: () => { offline = true; }, fetch: url => {
    if (offline && url.startsWith('/staff/receptions?')) throw new Error('list offline');
  } });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  assert.equal(h.el('call-success-view').hidden, true);
  assert.match(h.el('call-status-message').textContent, /最新一覧を確認できません/);
  await h.tick(10_000);
  assert.equal(h.el('call-status-view').hidden, false);
  assert.match(h.el('call-status-message').textContent, /最新一覧を確認できません/);
  assert.equal(h.postRequests().length, 1); h.controller.stop();
});

test('a refreshed list from a different database cannot turn a historical receipt into an expiring success', async () => {
  let switched = false;
  const nextDatabase = randomUUID();
  const h = callHarness({ post: () => { switched = true; }, fetch: url => {
    if (!switched || !url.startsWith('/staff/receptions?')) return undefined;
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? h.rows : [], view, { metadata: metadata({ database_id: nextDatabase }) }));
  } });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  await h.tick(10_000);
  assert.equal(h.el('call-success-view').hidden, true);
  assert.equal(h.el('call-status-view').hidden, false);
  assert.match(h.el('call-status-message').textContent, /最新一覧を確認できません/);
  assert.equal(h.postRequests().length, 1); h.controller.stop();
});

for (const failure of ['unknown', 'conflict', 'current-context']) {
  test(`${failure} never acquires a success timer or loses recovery information`, async () => {
    const h = callHarness({ post: () => {
      if (failure === 'unknown') throw new Error('lost');
      if (failure === 'conflict') return response({ detail: 'conflict' }, 409);
    }, context: (_, count) => { if (failure === 'current-context' && count >= 3) throw new Error('context offline'); } });
    await h.start(); await h.openCallMenu(); await h.chooseCall();
    const fixed = h.storage.getItem(CALL_JOURNAL_KEY);
    assert.ok(fixed);
    await h.tick(10_000);
    assert.equal(h.el('call-success-view').hidden, true); assert.equal(h.el('call-status-view').hidden, false);
    assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), fixed); assert.equal(h.postRequests().length, 1);
    h.controller.stop();
  });
}

test('an old success timer cannot erase the next success or a subsequent result-unknown panel', async () => {
  let attempts = 0;
  const h = callHarness({ post: () => { if (++attempts === 3) throw new Error('lost'); } });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  const oldTimers = [...h.timers.values()].filter(timer => timer.at === h.env.now() + 5_000).map(timer => timer.handler);
  h.rows[0] = { ...h.rows[0], status: 'absent', revision: 2, allowed_actions: ['call', 'return', 'seat', 'cancel'] };
  await h.controller.refresh(); await h.openCallMenu(); await h.chooseCall();
  // Even callbacks already queued before cancellation must be harmless.
  assert.ok(oldTimers.length); oldTimers.forEach(handler => handler()); await settle();
  assert.equal(h.el('call-success-view').hidden, false);
  h.rows[0] = { ...h.rows[0], status: 'absent', revision: 4, allowed_actions: ['call', 'return', 'seat', 'cancel'] };
  await h.controller.refresh(); await h.openCallMenu(); await h.chooseCall();
  const fixed = h.storage.getItem(CALL_JOURNAL_KEY);
  oldTimers.forEach(handler => handler()); await h.tick(5_000);
  assert.equal(h.el('call-status-view').hidden, false); assert.match(h.el('call-status-message').textContent, /結果不明/);
  assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), fixed); h.controller.stop();
});

test('focused or hovered success remains readable and gets five seconds after interaction ends', async () => {
  const h = callHarness(); await h.start(); await h.openCallMenu(); await h.chooseCall();
  h.el('call-success-close').focus();
  await h.tick(6_000); assert.equal(h.el('call-success-view').hidden, false);
  h.el('refresh-button').focus(); await h.el('call-success-view').emit('focusout');
  await h.el('call-success-view').emit('pointerenter');
  await h.tick(6_000); assert.equal(h.el('call-success-view').hidden, false);
  await h.el('call-success-view').emit('pointerleave');
  await h.tick(5_000); assert.equal(h.el('call-success-view').hidden, true); h.controller.stop();
});

test('logout clears success presentation and queued old timers cannot revive staff information after restart', async () => {
  const h = callHarness(); await h.start(); await h.openCallMenu(); await h.chooseCall();
  const timers = [...h.timers.values()].filter(timer => timer.at === h.env.now() + 5_000).map(timer => timer.handler);
  await h.controller.logout(); timers.forEach(handler => handler()); await settle();
  assertHidden(h); assert.equal(h.el('call-success-view').hidden, true);
  h.controller.stop();
  const next = callHarness({ rows: h.rows, storage: h.storage }); await next.start();
  assert.equal(next.el('call-success-view').hidden, true);
  assert.equal(next.postRequests().length, 0); next.controller.stop();
});

test('opening and closing the call menu with Escape sends nothing and restores focus', async () => {
  const h = callHarness(); await h.start(); await h.openCallMenu();
  assert.equal(h.postRequests().length, 0);
  assert.equal(h.el('row-menu').hidden, false);
  await h.doc.emit('keydown', { key: 'Escape' });
  assert.equal(h.el('row-menu').hidden, true);
  assert.equal(h.doc.activeElement, rowTrigger(rowItem(h)));
  assert.equal(h.postRequests().length, 0);
  h.controller.stop();
});

test('cooperating tabs cannot hold two call locks and another tab only acquires after the owner leaves', async () => {
  const held = new Set();
  const lockManager = { async request(name, options, callback) {
    assert.equal(options.ifAvailable, true);
    if (held.has(name)) return callback(null);
    held.add(name);
    try { return await callback({ name }); } finally { held.delete(name); }
  } };
  const first = callHarness({ lockManager }), second = callHarness({ lockManager });
  await first.start(); await second.start();
  await first.openCallMenu();
  await rowTrigger(rowItem(second)).emit('click');
  assert.equal(second.el('row-menu-items').children.flatMap(item => item.children).length, 0);
  first.controller.stop(); await settle();
  const third = callHarness({ lockManager }); await third.start(); await third.openCallMenu();
  assert.equal(third.actionButton.textContent, '呼出');
  second.controller.stop(); third.controller.stop();
});

for (const change of [ { status: 'called', revision: 1 }, { revision: 1 }, { adults: 3, total_people: 4 } ]) {
  test(`changed target at operation start is rejected: ${JSON.stringify(change)}`, async () => {
    const h = callHarness({ context: row => response({ metadata: metadata(), reception: { ...row, ...change } }) });
    await h.start(); await h.openCallMenu(); await h.chooseCall();
    assert.equal(h.postRequests().length, 0);
    h.controller.stop();
  });
}

for (const change of ['revision', 'write_epoch', 'database_id', 'status']) {
  test(`final context ${change} change invalidates the selected operation before UUID storage or POST`, async () => {
    const h = callHarness({ context: (row, count) => count === 2 ? response({ metadata: metadata(
      ['write_epoch', 'database_id'].includes(change) ? { [change]: randomUUID() } : {}),
    reception: { ...row, ...(change === 'revision' ? { revision: 1 } : change === 'status' ? { status: 'called' } : {}) } }) : undefined });
    await h.start(); await h.openCallMenu(); await h.chooseCall();
    assert.equal(h.postRequests().length, 0);
    assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null);

    h.controller.stop();
  });
}

test('background target changes invalidate an open menu while unchanged polling preserves it', async () => {
  const h = callHarness(); await h.start(); await h.openCallMenu();
  const item = rowItem(h);
  h.el('reception-scroll').scrollTop = 120;
  for (let i = 0; i < 25; i += 1) await h.controller.refresh();
  assert.equal(rowItem(h), item);
  assert.equal(h.el('reception-scroll').scrollTop, 120);
  assert.equal(h.el('row-menu').hidden, false);
  h.rows[0] = { ...h.rows[0], revision: 1 };
  await h.controller.refresh();

  await h.chooseCall(); assert.equal(h.postRequests().length, 0);
  h.controller.stop();
});

test('double action selection plus background five-second refresh creates one UUID and never aborts its POST', async () => {
  const pending = deferred(); let signal;
  const h = callHarness({ post: (body, request) => { signal = request.signal; return pending.promise; } });
  await h.start(); await h.openCallMenu();
  const sending = h.actionButton.emit('click'); await settle();
  await h.actionButton.emit('click');
  await h.tick(5_000);
  assert.equal(h.postRequests().length, 1); assert.equal(signal.aborted, false);
  assert.match(h.el('call-status-message').textContent, /送信中/);
  pending.reject(new Error('lost')); await sending; await settle();
  assert.match(h.el('call-status-message').textContent, /結果不明/);
  assert.ok(h.storage.getItem(CALL_JOURNAL_KEY));
  h.controller.stop();
});

test('lost POST receipt is recovered by authenticated lookup and fresh context without a second POST', async () => {
  const h = callHarness({ post: (body, _, rows, ledger) => {
    rows[0] = { ...rows[0], status: 'called', revision: 1, called_at: '2026-11-01T18:10:00+09:00' };
    ledger.set(body.operation_id, callReceipt(rows[0], body));
    throw new Error('response lost after commit');
  } });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  assert.match(h.el('call-status-message').textContent, /結果不明/);
  assert.ok(h.storage.getItem(CALL_JOURNAL_KEY));
  await h.el('call-lookup').emit('click'); await settle(); await settle();
  assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null);
  assert.equal(h.postRequests().length, 1);
  assert.equal(h.el('called-count').textContent, '1');
  h.controller.stop();
});

for (const status of [404, 503, 'network', 409]) {
  test(`lookup ${status} leaves unknown and new calls blocked, preserving the fixed request`, async () => {
    const h = callHarness({ post: () => { throw new Error('lost'); }, lookup: () => {
      if (status === 'network') throw new Error('offline');
      return response({ detail: 'not a saved operation receipt' }, status);
    } });
    await h.start(); await h.openCallMenu(); await h.chooseCall();
    const fixed = h.storage.getItem(CALL_JOURNAL_KEY);
    await h.el('call-lookup').emit('click'); await settle();
    assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), fixed);
    await rowTrigger(rowItem(h)).emit('click');
    assert.equal(h.el('row-menu-items').children.flatMap(item => item.children).length, 0);
    assert.equal(h.postRequests().length, 1);
    h.controller.stop();
  });
}

test('explicit retry after unknown uses byte-identical body, UUID and expected revision', async () => {
  let attempt = 0;
  const h = callHarness({ post: () => { if (++attempt === 1) throw new Error('lost'); } });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  await h.el('call-retry').emit('click'); await settle(); await settle();
  assert.equal(h.postRequests().length, 2);
  assert.equal(h.postRequests()[0].body, h.postRequests()[1].body);
  assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null);
  h.controller.stop();
});

for (const outcome of ['no_change', 'rejected']) {
  test(`lookup HTTP200 handles saved ${outcome} separately from lookup success`, async () => {
    const storage = new CallStorage(), ledger = new Map();
    const row = reception(1, { status: 'called', revision: 1, called_at: '2026-11-01T18:10:00+09:00' });
    const operation_id = randomUUID(), body = { operation_id, action: 'call', expected_revision: 1, write_epoch: metadata().write_epoch };
    createCallJournal({ storage, now: () => 0 }).begin({ operation_id, reception_id: row.id, request: body, database_id: metadata().database_id });
    ledger.set(operation_id, outcome === 'no_change' ? callReceipt(row, body, outcome) : {
      operation_id, reception_id: row.id, action: 'call', outcome, response_status: 409,
      recorded_at: row.called_at, resulting_revision: 1, reception: null, error: { code: 'revision_conflict', detail: 'changed' },
    });
    const h = callHarness({ rows: [row], storage, ledger }); await h.start(); await settle(); await settle();
    assert.equal(h.postRequests().length, 0);
    assert.equal(storage.getItem(CALL_JOURNAL_KEY), null);
    assert.match(h.el(outcome === 'no_change' ? 'call-success-message' : 'call-status-message').textContent, outcome === 'no_change' ? /変更なし/ : /拒否・競合/);
    await h.tick(10_000);
    if (outcome === 'rejected') {
      assert.equal(h.el('call-status-view').hidden, false);
      assert.match(h.el('call-status-message').textContent, /拒否・競合/);
    } else assert.equal(h.el('call-success-view').hidden, true);
    h.controller.stop();
  });
}

test('a generic POST409 is not a receipt and never changes revision for an automatic retry', async () => {
  const h = callHarness({ post: () => response({ detail: 'state conflict' }, 409) });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  const fixed = JSON.parse(h.storage.getItem(CALL_JOURNAL_KEY));
  assert.equal(fixed.request.expected_revision, 0);
  assert.match(h.el('call-status-message').textContent, /結果不明/);
  assert.equal(h.postRequests().length, 1); h.controller.stop();
});

test('reload/relogin restores one pending operation and looks up before allowing any new call', async () => {
  const storage = new CallStorage(), ledger = new Map();
  const first = callHarness({ storage, ledger, post: (body, _, rows) => {
    rows[0] = { ...rows[0], status: 'called', revision: 1, called_at: '2026-11-01T18:10:00+09:00' };
    ledger.set(body.operation_id, callReceipt(rows[0], body)); throw new Error('lost');
  } });
  await first.start(); await first.openCallMenu(); await first.chooseCall();
  const fixed = storage.getItem(CALL_JOURNAL_KEY);
  await first.controller.logout();
  assert.equal(storage.getItem(CALL_JOURNAL_KEY), fixed);
  assertHidden(first);
  first.controller.stop();
  const second = callHarness({ storage, ledger, rows: first.rows }); await second.start(); await settle(); await settle();
  assert.equal(second.postRequests().length, 0);
  assert.ok(second.requests.some(r => r.url.startsWith('/staff/operations/')));
  assert.equal(storage.getItem(CALL_JOURNAL_KEY), null);
  second.controller.stop();
});

for (const kind of ['unavailable', 'save-failure', 'corrupt', 'partial-missing']) {
  test(`storage ${kind} prevents all POSTs and retains a safe blocked state`, async () => {
    const storage = new CallStorage();
    if (kind === 'corrupt') storage.setItem(CALL_JOURNAL_KEY, '{');
    if (kind === 'partial-missing') storage.setItem('omachi.staff.call.guard.v1', 'pending:' + randomUUID());
    if (kind === 'unavailable') storage.fail = true;
    const h = callHarness({ storage }); await h.start();
    if (kind === 'save-failure') { await h.openCallMenu(); storage.fail = true; await h.chooseCall(); }
    else {
      await rowTrigger(rowItem(h)).emit('click');
      assert.equal(h.el('row-menu-items').children.flatMap(item => item.children).length, 0);
    }
    assert.equal(h.postRequests().length, 0);
    assert.match(h.el('call-status-message').textContent, /確認|停止/);
    h.controller.stop();
  });
}

test('storage becoming unreadable after successful save/readback still stops the actual POST', async () => {
  const storage = new CallStorage(); let payloadReads = 0;
  const read = storage.getItem.bind(storage);
  storage.getItem = key => {
    const raw = read(key);
    if (key === CALL_JOURNAL_KEY && raw && ++payloadReads === 2) storage.fail = true;
    return raw;
  };
  const h = callHarness({ storage }); await h.start(); await h.openCallMenu(); await h.chooseCall();
  assert.equal(h.postRequests().length, 0);
  assert.match(h.el('call-status-message').textContent, /確認|停止/);
  h.controller.stop();
});

test('expired recovery discards the fixed body and never auto-sends or treats it as failed', async () => {
  const storage = new CallStorage(), operation_id = randomUUID(), row = reception();
  createCallJournal({ storage, now: () => 0 }).begin({ operation_id, reception_id: row.id, database_id: metadata().database_id,
    request: { operation_id, action: 'call', expected_revision: 0, write_epoch: metadata().write_epoch } });
  const h = callHarness({ storage, now: CALL_RECOVERY_MS }); await h.start();
  assert.equal(JSON.parse(storage.getItem(CALL_JOURNAL_KEY)).state, 'manual');
  assert.equal(JSON.parse(storage.getItem(CALL_JOURNAL_KEY)).request, undefined);
  assert.equal(h.el('call-retry').hidden, true);
  assert.equal(h.requests.filter(r => r.url.startsWith('/staff/operations/')).length, 0);
  assert.match(h.el('call-status-message').textContent, /期限超過/);
  h.controller.stop();
});

test('old journal from a regenerated synthetic DB cannot be resent or automatically released', async () => {
  const storage = new CallStorage(), operation_id = randomUUID();
  createCallJournal({ storage, now: () => 0 }).begin({ operation_id, reception_id: reception().id, database_id: randomUUID(),
    request: { operation_id, action: 'call', expected_revision: 0, write_epoch: metadata().write_epoch } });
  const h = callHarness({ storage }); await h.start(); await settle();
  await h.el('call-retry').emit('click'); await settle();
  assert.equal(h.postRequests().length, 0);
  assert.ok(storage.getItem(CALL_JOURNAL_KEY)); h.controller.stop();
});

for (const opts of [{ callLab: false }, { lockAvailable: false }, { locksSupported: false }]) {
  test(`non-call or unowned tab cannot offer a call: ${JSON.stringify(opts)}`, async () => {
    const h = callHarness(opts); await h.start(); await rowTrigger(rowItem(h)).emit('click');
    assert.equal(h.el('row-menu-items').children.flatMap(item => item.children).length, 0);
    assert.equal(h.postRequests().length, 0); h.controller.stop();
  });
}

for (const status of ['called', 'seated', 'cancelled']) {
  test(`fresh call is never offered for ${status}`, async () => {
    // A called active row has no call action. Terminal rows are rejected from
    // active listings by the existing fail-closed list validation.
    const h = callHarness({ row: { status, allowed_actions: ['seat', 'cancel'] } }); await h.start();
    if (status === 'called') {
      await rowTrigger(rowItem(h)).emit('click');
      assert.equal(h.el('row-menu-items').children.flatMap(item => item.children).length, 0);
    } else assertHidden(h);
    assert.equal(h.postRequests().length, 0); h.controller.stop();
  });
}

test('POST timeout stays unknown and its late successful response cannot overwrite the current UI', async () => {
  const pending = deferred(); let signal, fixed;
  const h = callHarness({ post: (body, request) => { fixed = body; signal = request.signal; return pending.promise; } });
  await h.start(); await h.openCallMenu();
  const sending = h.actionButton.emit('click'); await settle();
  await h.tick(15_000); await sending; await settle();
  assert.equal(signal.aborted, true);
  assert.match(h.el('call-status-message').textContent, /結果不明/);
  const row = { ...h.rows[0], status: 'called', called_at: '2026-11-01T18:10:00+09:00' };
  pending.resolve(response(callReceipt(row, fixed))); await settle();
  assert.equal(h.el('waiting-count').textContent, '1');
  assert.ok(h.storage.getItem(CALL_JOURNAL_KEY));
  assert.equal(h.postRequests().length, 1); h.controller.stop();
});

test('lookup in flight survives polling without starting another write or losing its ID', async () => {
  const pending = deferred(); let signal;
  const h = callHarness({ post: () => { throw new Error('lost'); }, lookup: (_, request) => { signal = request.signal; return pending.promise; } });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  const checking = h.el('call-lookup').emit('click'); await settle();
  await h.tick(5_000);
  assert.equal(signal.aborted, false);
  assert.equal(h.postRequests().length, 1);
  pending.resolve(response({ detail: 'no receipt' }, 404)); await checking; await settle();
  assert.ok(h.storage.getItem(CALL_JOURNAL_KEY)); h.controller.stop();
});

test('a received receipt with failed current-context lookup disables retry until safe lookup completes', async () => {
  const h = callHarness({ context: (_, count) => { if (count === 3) throw new Error('context offline'); } });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  assert.match(h.el('call-status-message').textContent, /受領済み/);
  assert.equal(h.el('call-retry').hidden, true);
  assert.ok(h.storage.getItem(CALL_JOURNAL_KEY));
  await h.el('call-retry').emit('click'); assert.equal(h.postRequests().length, 1);
  await h.el('call-lookup').emit('click'); await settle(); await settle();
  assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null);
  assert.equal(h.postRequests().length, 1); h.controller.stop();
});

test('the current row comes from context/list reads, even when a historical receipt says called', async () => {
  const h = callHarness({ post: (body, _, rows, ledger) => {
    const historical = { ...rows[0], status: 'called', revision: 1, called_at: '2026-11-01T18:10:00+09:00' };
    ledger.set(body.operation_id, callReceipt(historical, body));
    rows[0] = { ...historical, status: 'absent', revision: 2, allowed_actions: ['call', 'return', 'seat', 'cancel'] };
    throw new Error('response lost');
  } });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  await h.el('call-lookup').emit('click'); await settle(); await settle();
  assert.equal(h.el('absent-tab-count').textContent, '1');
  assert.equal(h.el('called-count').textContent, '0');
  assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null); h.controller.stop();
});

test('logout during an in-flight POST hides details and retains journal despite a late response', async () => {
  const pending = deferred(); let body;
  const h = callHarness({ post: b => { body = b; return pending.promise; } });
  await h.start(); await h.openCallMenu();
  const sending = h.actionButton.emit('click'); await settle();
  await h.controller.logout(); assertHidden(h);
  const record = h.storage.getItem(CALL_JOURNAL_KEY);
  assert.ok(record); assert.equal(h.el('call-status-view').hidden, true);
  pending.resolve(response(callReceipt({ ...h.rows[0], status: 'called', called_at: '2026-11-01T18:10:00+09:00' }, body)));
  await sending; await settle();
  assertHidden(h); assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), record);
  h.controller.stop();
});

for (const status of [401, 403]) {
  test(`operation auth ${status} closes information and retains unknown for authenticated recovery`, async () => {
    const h = callHarness({ post: () => response({ detail: 'auth failure' }, status) });
    await h.start(); await h.openCallMenu(); await h.chooseCall();
    assertHidden(h); assert.ok(h.storage.getItem(CALL_JOURNAL_KEY));
    assert.ok(h.redirects.includes('/staff/login'));
    assert.equal(h.el('call-status-view').hidden, true); h.controller.stop();
  });
}

test('background failure during explicit retry preflight prevents the retry POST', async () => {
  const pending = deferred(); let listFail = false;
  const h = callHarness({ post: () => { throw new Error('lost'); },
    context: (_, count) => count === 3 ? pending.promise : undefined,
    fetch: url => { if (listFail && url.startsWith('/staff/receptions?')) throw new Error('list offline'); } });
  await h.start(); await h.openCallMenu(); await h.chooseCall();
  const retrying = h.el('call-retry').emit('click'); await settle();
  listFail = true; await h.controller.refresh(); assertHidden(h);
  pending.resolve(response({ metadata: metadata(), reception: h.rows[0] })); await retrying; await settle();
  assert.equal(h.postRequests().length, 1);
  assert.ok(h.storage.getItem(CALL_JOURNAL_KEY)); h.controller.stop();
});

for (const status of ['waiting', 'called']) {
  test(`call+absent API lab stays read-only in the existing UI (${status})`, async () => {
    const row = reception(1, { status, allowed_actions: status === 'waiting' ? ['call'] : ['absent'] });
    const h = callHarness({ rows: [row], fetch: url => {
      if (!url.startsWith('/staff/receptions?')) return undefined;
      const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
      return { ...response(listing(view === 'active' ? [row] : [], view)), headers: { get: name => ({
        'X-Omachi-Preview': 'synthetic', 'X-Omachi-Preview-Actions': 'call,absent',
      })[name] ?? null } };
    } });
    await h.start();
    assert.equal(h.el('staff-view').hidden, false);
    await rowTrigger(rowItem(h)).emit('click');
    assert.equal(h.el('row-menu-items').children.flatMap(item => item.children).length, 0);
    await h.controller.refresh();
    assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null);
    assert.equal(h.storage.getItem('omachi.staff.call.guard.v1'), null);
    assertReadOnlyRequests(h.requests);
    h.controller.stop();
  });
}

test('missing call capability on any page leaves the general UI read-only even with a writable metadata flag', async () => {
  const h = callHarness({ fetch: url => {
    if (!url.startsWith('/staff/receptions?')) return undefined;
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return { ...response(listing(view === 'active' ? [reception()] : [], view)), headers: { get: () => null } };
  } });
  await h.start(); assert.equal(h.postRequests().length, 0);
  assert.equal(h.el('staff-view').hidden, false);
  await rowTrigger(rowItem(h)).emit('click');
  assert.equal(h.el('row-menu-items').children.flatMap(item => item.children).length, 0);
  assert.equal(h.storage.getItem('omachi.staff.call.guard.v1'), null);
  assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null); h.controller.stop();
});

for (const mutation of [
  r => ({ ...r, action: 'seat' }), r => ({ ...r, operation_id: randomUUID() }), r => ({ ...r, reception_id: randomUUID() }),
  r => ({ ...r, response_status: 409 }), r => ({ ...r, resulting_revision: 0 }),
  r => ({ ...r, outcome: 'unexpected' }), r => ({ ...r, extra: true }),
  r => ({ ...r, reception: { ...r.reception, actual_wait_minutes: 1 } }),
  r => ({ ...r, reception: { ...r.reception, prediction: { ...r.reception.prediction, minutes: -1 } } }),
]) {
  test(`a malformed saved receipt is never treated as a confirmed call: ${mutation.toString()}`, () => {
    const body = { operation_id: randomUUID(), action: 'call', expected_revision: 0, write_epoch: metadata().write_epoch };
    const row = reception(1, { status: 'called', revision: 1, called_at: '2026-11-01T18:10:00+09:00' });
    const record = { operation_id: body.operation_id, reception_id: row.id, request: body };
    const valid = callReceipt(row, body);
    assert.equal(validCallReceipt(valid, record), true);
    assert.equal(validCallReceipt(mutation(valid), record), false);
  });
}

test('unauthenticated management startup never requests or displays reception information', async () => {
  const h = harness({ authenticated: false });
  await h.start();
  assertHidden(h);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, '/staff/session');
  assertReadOnlyRequests(h.requests);
});

test('an existing cookie session restores the management screen with exact counts and reception order', async () => {
  const rows = [reception(1), reception(2, { status: 'called' }), reception(3, { status: 'absent' })];
  const h = harness({ rows });
  await h.start();
  assert.equal(h.el('staff-view').hidden, false);
  assert.equal(h.el('waiting-count').textContent, '1');
  assert.equal(h.el('called-count').textContent, '1');
  assert.equal(h.el('absent-tab-count').textContent, '1');
  const text = h.el('active-list').textContent;
  assert.ok(text.indexOf('001') < text.indexOf('002'));
  assert.doesNotMatch(text, /003|不在/);
  assert.match(h.el('absent-list').textContent, /003.*不在/);
  assert.match(text, /2026/);
  assert.match(text, /大人/);
  assert.match(text, /子供|子ども/);
  assert.match(text, /待機|待ち/);
  assert.match(text, /呼出|呼び出/);
  assertReadOnlyRequests(h.requests);
});

test('a login page with an existing session redirects without loading any reception rows', async () => {
  const h = harness({ page: 'login' });
  await h.start();
  assert.deepEqual(h.redirects, ['/staff/']);
  assert.equal(h.requests.length, 1);
  assertHidden(h);
});

test('login sends the manually entered credential only in its dedicated header and immediately clears the input', async () => {
  const pending = deferred();
  const secret = 'synthetic-human-entered-token-never-persisted';
  const h = harness({ page: 'login', fetch: async (_url, options) => {
    if (options.method === 'POST') return pending.promise;
    return response({}, 401);
  } });
  await h.start();
  h.el('staff-token').value = secret;
  const loggingIn = h.controller.login({ preventDefault() {} });
  await settle();
  assert.equal(h.el('staff-token').value, '');
  const request = h.requests.find(item => item.method === 'POST');
  assert.equal(request.url, '/staff/session');
  assert.equal(request.headers['X-Staff-Token'], secret);
  assert.equal(request.body, undefined);
  assert.doesNotMatch(h.el('login-message').textContent, new RegExp(secret));
  pending.resolve(response(session()));
  await loggingIn;
  assert.deepEqual(h.redirects, ['/staff/']);
  assertReadOnlyRequests(h.requests);
});

test('repeated login submissions produce a single credential request', async () => {
  const pending = deferred();
  const h = harness({ page: 'login', fetch: async (_url, options) => options.method === 'POST' ? pending.promise : response({}, 401) });
  await h.start();
  h.el('staff-token').value = 'synthetic-credential';
  const first = h.controller.login();
  await settle();
  h.el('staff-token').value = 'another-value-must-not-be-sent';
  const second = h.controller.login();
  await settle();
  assert.equal(h.requests.filter(item => item.method === 'POST').length, 1);
  assert.equal(h.el('login-button').disabled, true);
  pending.resolve(response(session()));
  await Promise.all([first, second]);
});

test('empty login input never sends an authentication request', async () => {
  const h = harness({ page: 'login', authenticated: false });
  await h.start();
  h.el('staff-token').value = '';
  await h.controller.login();
  assert.equal(h.requests.filter(item => item.method === 'POST').length, 0);
  assertHidden(h);
});

for (const status of [401, 403, 429, 503]) {
  test(`login HTTP ${status} stays closed and never reflects the server's credential-bearing text`, async () => {
    const secret = 'synthetic-secret-for-failure-case';
    const h = harness({ page: 'login', fetch: async (_url, options) => options.method === 'POST'
      ? response({ detail: `${secret}<img src=x onerror=alert(1)>` }, status) : response({}, 401) });
    await h.start();
    h.el('staff-token').value = secret;
    await h.controller.login();
    assertHidden(h);
    assert.equal(h.el('staff-token').value, '');
    assert.notEqual(h.el('login-message').textContent, '');
    assert.doesNotMatch(h.el('login-message').textContent, /synthetic-secret|<img|onerror/);
    assert.deepEqual(h.redirects, []);
  });
}

const rowItem = (h, index = 0, view = 'active') => h.el(`${view}-list`).children[index];
const rowTrigger = item => item.children[4];
const seatedRow = (index = 1) => reception(index, { status: 'seated', revision: 1,
  seated_at: '2026-11-01T18:25:00+09:00', actual_wait_minutes: 25,
  allowed_actions: [], valid_seating_event: { event_id: index + 1000, from_status: 'waiting', to_status: 'seated',
    occurred_at: '2026-11-01T18:25:00+09:00', correctable: false } });

async function swipe(h, item, { dx = 95, dy = 4, pointerType = 'touch', end = 'pointerup',
  pointerId = 1, isPrimary = true, during = null } = {}) {
  const start = { pointerId, pointerType, isPrimary, button: 0, clientX: 30, clientY: 40 };
  await item.emit('pointerdown', start);
  await item.emit('pointermove', { ...start, clientX: 30 + dx, clientY: 40 + dy });
  if (during) await during(start);
  await item.emit(end, { ...start, clientX: 30 + dx, clientY: 40 + dy });
}

test('compact rows preserve order and show main ticket, full date and all five cells', async () => {
  const rows = [reception(1, { status: 'absent' }), reception(2), reception(3, { status: 'called' })];
  const h = harness({ rows });
  await h.start();
  assert.equal(h.el('active-list').children.length, 2);
  assert.equal(h.el('absent-list').children.length, 1);
  for (const [index, row] of rows.entries()) {
    const item = rowItem(h, row.status === 'absent' ? 0 : index - 1, row.status === 'absent' ? 'absent' : 'active');
    assert.equal(item.children.length, 5);
    assert.equal(item.dataset.receptionId, row.id);
    assert.equal(item.children[0].children[0].textContent, `${String(index + 1).padStart(3, '0')}番`);
    assert.equal(item.children[0].children[1].textContent, '2026/11/01');
    assert.match(item.children[1].textContent, /受付.*18:00/);
    assert.match(item.children[2].textContent, /大人 2名.*子供 1名/);
    assert.equal(rowTrigger(item).textContent, '⋯');
    assert.equal(rowTrigger(item).getAttribute('aria-controls'), 'row-menu');
  }
  assert.equal(h.el('waiting-count').textContent, '1');
  assert.equal(h.el('called-count').textContent, '1');
  assert.equal(h.el('absent-tab-count').textContent, '1');
  h.controller.stop();
});

test('recent tab keeps full dated ticket and distinguishes seating from intake time', async () => {
  const h = harness({ rows: [reception(2)], recent: [seatedRow(1)] });
  await h.start();
  const count = h.requests.length;
  await h.el('recent-tab').emit('click');
  assert.equal(h.el('active-panel').hidden, true);
  assert.equal(h.el('recent-panel').hidden, false);
  assert.equal(h.el('recent-tab').getAttribute('aria-selected'), 'true');
  assert.match(rowItem(h, 0, 'recent').children[1].textContent, /案内.*18:25.*受付.*18:00/);
  assert.equal(h.requests.length, count, 'tab changes must not initiate a different API path');
  await h.el('active-tab').emit('click');
  assert.equal(h.el('active-panel').hidden, false);
  assert.equal(h.el('recent-panel').hidden, true);
});

test('right swipe and ellipsis open the same inert preview and never call a write API', async () => {
  const h = harness({ rows: [reception()] });
  await h.start();
  const item = rowItem(h), count = h.requests.length;
  await rowTrigger(item).emit('click');
  const title = h.el('row-menu-title').textContent, contents = h.el('row-menu-items').textContent;
  assert.match(contents, /呼出.*案内完了.*受付取消/);
  assert.ok(h.el('row-menu-items').children.every(child => child.tagName === 'LI'));
  assert.match(contents, /未接続・実行不可/);
  await h.el('row-menu-close').emit('click');
  await swipe(h, item);
  assert.equal(h.el('row-menu').hidden, false);
  assert.equal(h.el('row-menu-title').textContent, title);
  assert.equal(h.el('row-menu-items').textContent, contents);
  assert.equal(h.requests.length, count);
  assertReadOnlyRequests(h.requests);
});

test('only server-provided known operation names are previewed; legacy offers no fake controls', async () => {
  const h = harness({ rows: [reception(1, { allowed_actions: [] }), reception(2, {
    allowed_actions: ['absent', 'absent', '<img src=x onerror=alert(1)>', null, { toString: 'seat' }] })] });
  await h.start();
  await rowTrigger(rowItem(h)).emit('click');
  assert.match(h.el('row-menu-items').textContent, /利用可能な操作項目はありません/);
  await rowTrigger(rowItem(h, 1)).emit('click');
  assert.equal(h.el('row-menu-items').children.length, 1);
  assert.match(h.el('row-menu-items').textContent, /不在/);
  assert.doesNotMatch(h.el('row-menu-items').textContent, /img|onerror/);
});

test('a different row replaces the previous menu and Escape returns focus to its ellipsis', async () => {
  const h = harness({ rows: [reception(1), reception(2)] });
  await h.start();
  const first = rowTrigger(rowItem(h)), second = rowTrigger(rowItem(h, 1));
  await first.emit('click');
  assert.equal(first.getAttribute('aria-expanded'), 'true');
  await second.emit('click');
  assert.equal(first.getAttribute('aria-expanded'), 'false');
  assert.equal(second.getAttribute('aria-expanded'), 'true');
  assert.match(h.el('row-menu-title').textContent, /002/);
  await h.doc.emit('keydown', { key: 'Escape' });
  assert.equal(h.el('row-menu').hidden, true);
  assert.equal(second.getAttribute('aria-expanded'), 'false');
  assert.equal(h.doc.activeElement, second);
});

for (const reason of ['close', 'outside', 'focus', 'scroll', 'blur', 'tab']) {
  test(`preview closes safely on ${reason}`, async () => {
    const h = harness({ rows: [reception()] });
    await h.start();
    await rowTrigger(rowItem(h)).emit('click');
    if (reason === 'close') await h.el('row-menu-close').emit('click');
    if (reason === 'outside') await h.el('refresh-button').emit('pointerdown', { pointerType: 'mouse', pointerId: 1 });
    if (reason === 'focus') h.el('refresh-button').focus();
    if (reason === 'scroll') await h.el('reception-scroll').emit('scroll');
    if (reason === 'blur') await h.env.emit('blur');
    if (reason === 'tab') await h.el('recent-tab').emit('click');
    assert.equal(h.el('row-menu').hidden, true);
    assert.equal(h.el('row-menu-items').textContent, '');
  });
}

for (const [name, options] of [
  ['vertical', { dx: 8, dy: 100 }], ['diagonal', { dx: 100, dy: 45 }],
  ['short', { dx: 35, dy: 0 }], ['left', { dx: -100, dy: 2 }],
  ['cancelled', { end: 'pointercancel' }], ['lost capture', { end: 'lostpointercapture' }],
  ['left row', { end: 'pointerleave' }], ['secondary finger', { isPrimary: false }],
  ['mouse drag', { pointerType: 'mouse' }],
]) {
  test(`${name} gesture never opens a menu or performs a reception action`, async () => {
    const h = harness({ rows: [reception()] });
    await h.start();
    const count = h.requests.length;
    await swipe(h, rowItem(h), options);
    assert.equal(h.el('row-menu').hidden, true);
    assert.equal(h.el('detail-view').hidden, true);
    assert.equal(h.requests.length, count);
  });
}

test('a cancelled diagonal path cannot become a swipe by ending on the original height', async () => {
  const h = harness({ rows: [reception()] });
  await h.start();
  const item = rowItem(h), start = { pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0, clientX: 20, clientY: 20 };
  await item.emit('pointerdown', start);
  await item.emit('pointermove', { ...start, clientX: 70, clientY: 70 });
  await item.emit('pointerup', { ...start, clientX: 150 });
  assert.equal(h.el('row-menu').hidden, true);
});

test('multi-touch and long interrupted movement are rejected', async () => {
  for (const kind of ['multi', 'long']) {
    const h = harness({ rows: [reception()] });
    await h.start();
    await swipe(h, rowItem(h), { during: async () => {
      if (kind === 'multi') await h.doc.emit('pointerdown', { pointerId: 2, pointerType: 'touch', isPrimary: false });
      else await h.tick(1300);
    } });
    assert.equal(h.el('row-menu').hidden, true);
  }
});

test('a pen swipe opens the preview only after release and suppresses the following pointer click', async () => {
  const h = harness({ rows: [reception()] });
  await h.start();
  const item = rowItem(h), start = { pointerId: 1, pointerType: 'pen', isPrimary: true, button: 0, clientX: 20, clientY: 20 };
  await item.emit('pointerdown', start);
  await item.emit('pointermove', { ...start, clientX: 120 });
  assert.equal(h.el('row-menu').hidden, true);
  await item.emit('pointerup', { ...start, clientX: 120 });
  assert.equal(h.el('row-menu').hidden, false);
  const count = h.requests.length;
  const click = await item.children[0].emit('click', { detail: 1 });
  assert.equal(click.defaultPrevented, true);
  assert.equal(h.el('detail-view').hidden, true);
  assert.equal(h.requests.length, count);
  await h.el('row-menu-close').emit('click', { detail: 0 }); // Keyboard activation is never suppressed.
  assert.equal(h.el('row-menu').hidden, true);
});

test('tab keyboard arrows, Home and End use roving focus without altering counts', async () => {
  const h = harness({ rows: [reception()] });
  await h.start();
  await h.el('active-tab').emit('keydown', { key: 'ArrowRight' });
  assert.equal(h.doc.activeElement, h.el('absent-tab'));
  assert.equal(h.el('active-tab').getAttribute('tabindex'), '-1');
  await h.el('absent-tab').emit('keydown', { key: 'ArrowRight' });
  assert.equal(h.doc.activeElement, h.el('recent-tab'));
  await h.el('recent-tab').emit('keydown', { key: 'ArrowRight' });
  assert.equal(h.doc.activeElement, h.el('active-tab'));
  await h.el('active-tab').emit('keydown', { key: 'ArrowLeft' });
  assert.equal(h.doc.activeElement, h.el('recent-tab'));
  await h.el('recent-tab').emit('keydown', { key: 'Home' });
  assert.equal(h.doc.activeElement, h.el('active-tab'));
  await h.el('active-tab').emit('keydown', { key: 'End' });
  assert.equal(h.doc.activeElement, h.el('recent-tab'));
  assert.equal(h.el('waiting-count').textContent, '1');
});

test('scroll positions are local to each tab and number details still use the matching ID', async () => {
  const h = harness({ rows: [reception()], recent: [seatedRow(2)] });
  await h.start();
  h.el('reception-scroll').scrollTop = 240;
  await h.el('recent-tab').emit('click');
  assert.equal(h.el('reception-scroll').scrollTop, 0);
  h.el('reception-scroll').scrollTop = 60;
  await rowItem(h, 0, 'recent').children[0].emit('click');
  assert.match(h.el('detail-title').textContent, /002/);
  assert.equal(h.requests.at(-1).url, `/staff/receptions/${seatedRow(2).id}/operation-context`);
  await h.doc.emit('keydown', { key: 'Escape' });
  assert.equal(h.doc.activeElement, rowItem(h, 0, 'recent').children[0]);
  await h.el('active-tab').emit('click');
  assert.equal(h.el('reception-scroll').scrollTop, 240);
});

test('background refresh preserves a verified menu and keyboard focus until a consistent response arrives', async () => {
  let delayed = false;
  const pending = deferred(), row = reception();
  const h = harness({ fetch: async url => {
    if (url === '/staff/session') return delayed ? pending.promise : response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [row] : [], view));
  } });
  await h.start();
  const oldTrigger = rowTrigger(rowItem(h));
  await oldTrigger.emit('click');
  const focus = h.doc.activeElement, items = [...h.el('row-menu-items').children];
  delayed = true;
  const refresh = h.controller.refresh();
  await settle();
  assert.equal(h.el('row-menu').hidden, false);
  assert.equal(h.doc.activeElement, focus);
  assert.equal(h.el('staff-view').dataset.state, 'current');
  assert.equal(h.el('status-message').hidden, true);
  assert.equal(h.el('recent-tab').disabled, false);
  pending.resolve(response(session()));
  await refresh;
  assert.equal(h.el('row-menu').hidden, false);
  assert.deepEqual(h.el('row-menu-items').children, items);
  assert.equal(rowTrigger(rowItem(h)), oldTrigger);
  assert.equal(h.doc.activeElement, focus);
  assert.equal(h.el('recent-tab').disabled, false);
});

test('twenty-five identical polling cycles preserve every row, counts, focus, menu and scroll without loading flicker', async () => {
  const h = harness({ rows: [reception(1), reception(2)], recent: [seatedRow(3)] });
  await h.start();
  const active = [...h.el('active-list').children], recent = [...h.el('recent-list').children];
  const counts = ['waiting-count', 'called-count', 'absent-tab-count'].map(id => h.el(id).textContent);
  await rowTrigger(active[0]).emit('click');
  const focus = h.doc.activeElement, menuItems = [...h.el('row-menu-items').children];
  h.el('reception-scroll').scrollTop = 230;
  // A DOM replacement, even with equivalent content, must fail this test.
  for (const id of ['active-list', 'recent-list', 'row-menu-items']) {
    h.el(id).replaceChildren = h.el(id).insertBefore = h.el(id).removeChild = () => assert.fail(`Unnecessary mutation: ${id}`);
  }
  for (let cycle = 0; cycle < 25; cycle += 1) {
    await h.tick(5000);
    assert.deepEqual(h.el('active-list').children, active);
    assert.deepEqual(h.el('recent-list').children, recent);
    assert.deepEqual(h.el('row-menu-items').children, menuItems);
    assert.deepEqual(['waiting-count', 'called-count', 'absent-tab-count'].map(id => h.el(id).textContent), counts);
    assert.equal(h.el('staff-view').dataset.state, 'current');
    assert.equal(h.el('status-message').hidden, true);
    assert.equal(h.el('row-menu').hidden, false);
    assert.equal(h.doc.activeElement, focus);
    assert.equal(h.el('reception-scroll').scrollTop, 230);
  }
  assert.equal(h.requests.filter(req => req.url === '/staff/session').length, 26);
  assertReadOnlyRequests(h.requests);
});

test('adding or updating a different reception keeps the menu target and all unchanged DOM rows', async () => {
  let rows = [reception(1), reception(2)], version = 'a';
  const h = harness({ fetch: async url => {
    if (url === '/staff/session') return response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? rows : [], view, { metadata: metadata({ view_version: version.repeat(64) }) }));
  } });
  await h.start();
  const first = rowItem(h), second = rowItem(h, 1);
  await rowTrigger(first).emit('click');
  const focus = h.doc.activeElement;
  rows = [...rows, reception(3)]; version = 'b';
  await h.controller.refresh();
  assert.equal(h.el('row-menu').hidden, false);
  assert.equal(rowItem(h), first); assert.equal(rowItem(h, 1), second);
  assert.equal(h.el('waiting-count').textContent, '3');
  const third = rowItem(h, 2);
  rows = [rows[0], reception(2, { status: 'called', revision: 1 }), rows[2]]; version = 'c';
  await h.controller.refresh();
  assert.equal(rowItem(h), first); assert.equal(rowItem(h, 2), third);
  assert.notEqual(rowItem(h, 1), second);
  assert.equal(h.el('waiting-count').textContent, '2');
  assert.equal(h.el('called-count').textContent, '1');
  assert.equal(h.el('row-menu').hidden, false);
  assert.equal(h.doc.activeElement, focus);
  assertReadOnlyRequests(h.requests);
});

for (const change of ['revision', 'state', 'removal', 'seating', 'epoch', 'database', 'mode', 'flag', 'actions', 'event']) {
  test(`background refresh invalidates a menu when its target or protection changes: ${change}`, async () => {
    let rows = [reception()], recent = [], meta = metadata();
    const h = harness({ fetch: async url => {
      if (url === '/staff/session') return response(session());
      const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
      return response(listing(view === 'active' ? rows : recent, view, { metadata: meta }));
    } });
    await h.start();
    await rowTrigger(rowItem(h)).emit('click');
    if (change === 'revision') rows = [reception(1, { revision: 1 })];
    if (change === 'state') rows = [reception(1, { status: 'called' })];
    if (change === 'removal') rows = [];
    if (change === 'seating') { rows = []; recent = [seatedRow(1)]; }
    if (change === 'epoch') meta = metadata({ write_epoch: 'd'.repeat(36) });
    if (change === 'database') meta = metadata({ database_id: 'e'.repeat(36) });
    if (change === 'mode') meta = metadata({ staff_write_mode: 'legacy', write_enabled: false });
    if (change === 'flag') meta = metadata({ seat_corrections_enabled: true });
    if (change === 'actions') rows = [reception(1, { allowed_actions: [] })];
    if (change === 'event') rows = [reception(1, { called_at: '2026-11-01T18:05:00+09:00' })];
    await h.controller.refresh();
    assert.equal(h.el('row-menu').hidden, true);
    assert.equal(h.el('row-menu-items').textContent, '');
    assertReadOnlyRequests(h.requests);
  });
}

test('scrolling during a delayed background fetch keeps the position reached by the staff member', async () => {
  let delayed = false;
  const pending = deferred();
  const h = harness({ fetch: async url => {
    if (url === '/staff/session') return delayed ? pending.promise : response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  h.el('reception-scroll').scrollTop = 90;
  rowItem(h).children[0].focus();
  const focus = h.doc.activeElement;
  delayed = true;
  const update = h.controller.refresh(); await settle();
  h.el('reception-scroll').scrollTop = 340;
  await h.el('reception-scroll').emit('scroll');
  pending.resolve(response(session())); await update;
  assert.equal(h.el('reception-scroll').scrollTop, 340);
  assert.equal(h.doc.activeElement, focus);
});

test('a touch swipe can cross an unchanged background update without restarting its gesture', async () => {
  const h = harness({ rows: [reception()] });
  await h.start();
  const item = rowItem(h), start = { pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0, clientX: 20, clientY: 20 };
  await item.emit('pointerdown', start);
  await item.emit('pointermove', { ...start, clientX: 70 });
  await h.controller.refresh();
  assert.equal(rowItem(h), item);
  await item.emit('pointerup', { ...start, clientX: 120 });
  assert.equal(h.el('row-menu').hidden, false);
  assertReadOnlyRequests(h.requests);
});

test('a changed target cancels a touch swipe that began before its background update', async () => {
  const rows = [reception()];
  const h = harness({ rows }); await h.start();
  const item = rowItem(h), start = { pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0, clientX: 20, clientY: 20 };
  await item.emit('pointerdown', start);
  rows[0] = reception(1, { revision: 1 });
  await h.controller.refresh();
  await item.emit('pointerup', { ...start, clientX: 120 });
  assert.equal(h.el('row-menu').hidden, true);
});

test('unchanged background polling does not cancel a pending authenticated detail read', async () => {
  const pending = deferred(), row = reception();
  let detailSignal;
  const h = harness({ fetch: async (url, options) => {
    if (url === '/staff/session') return response(session());
    if (url.endsWith('/operation-context')) { detailSignal = options.signal; return pending.promise; }
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [row] : [], view));
  } });
  await h.start();
  const opening = h.controller.openDetail(row.id); await settle();
  await h.controller.refresh();
  assert.equal(detailSignal.aborted, false);
  pending.resolve(response({ metadata: metadata(), reception: row }));
  assert.equal(await opening, true);
  assert.equal(h.el('detail-view').hidden, false);
  assertReadOnlyRequests(h.requests);
});

test('equivalent JSON property and action order does not replace a row or close its menu', async () => {
  const rows = [reception()];
  const h = harness({ rows }); await h.start();
  const item = rowItem(h); await rowTrigger(item).emit('click');
  rows[0] = Object.fromEntries(Object.entries(reception(1, { allowed_actions: ['seat', 'cancel', 'call'] })).reverse());
  await h.controller.refresh();
  assert.equal(rowItem(h), item);
  assert.equal(h.el('row-menu').hidden, false);
});

test('all pages are validated before one atomic list update, preserving a last-page menu during additions', async () => {
  let rows = Array.from({ length: 151 }, (_, index) => reception(index + 1));
  let version = 'a', delayed = false;
  const secondPage = deferred();
  const h = harness({ fetch: async url => {
    if (url === '/staff/session') return response(session());
    const params = new URL(url, 'https://staff.localhost').searchParams, view = params.get('view');
    const meta = metadata({ view_version: version.repeat(64) });
    if (view !== 'active') return response(listing([], view, { metadata: meta }));
    if (params.has('cursor')) return delayed ? secondPage.promise : response(listing(rows.slice(100), view, { metadata: meta }));
    return response(listing(rows.slice(0, 100), view, { metadata: meta, has_more: true, next_cursor: 'page-two' }));
  } });
  await h.start();
  const oldRows = [...h.el('active-list').children];
  await rowTrigger(oldRows[150]).emit('click');
  rows = [...rows, reception(152)]; version = 'b'; delayed = true;
  const updating = h.controller.refresh(); await settle();
  assert.deepEqual(h.el('active-list').children, oldRows);
  assert.equal(h.el('waiting-count').textContent, '151');
  assert.equal(h.el('row-menu').hidden, false);
  secondPage.resolve(response(listing(rows.slice(100), 'active', { metadata: metadata({ view_version: 'b'.repeat(64) }) })));
  await updating;
  assert.equal(h.el('waiting-count').textContent, '152');
  assert.equal(h.el('active-list').children.length, 152);
  for (let index = 0; index < oldRows.length; index += 1) assert.equal(rowItem(h, index), oldRows[index]);
  assert.equal(h.el('row-menu').hidden, false);
  assert.match(h.el('row-menu-title').textContent, /151/);
  assertReadOnlyRequests(h.requests);
});

test('a swipe released during a pending background fetch opens and retains its verified read-only menu', async () => {
  let delayed = false;
  const pending = deferred();
  const h = harness({ fetch: async url => {
    if (url === '/staff/session') return delayed ? pending.promise : response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  const item = rowItem(h), start = { pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0, clientX: 20, clientY: 20 };
  await item.emit('pointerdown', start);
  delayed = true;
  const updating = h.controller.refresh(); await settle();
  await item.emit('pointerup', { ...start, clientX: 120 });
  assert.equal(h.el('row-menu').hidden, false);
  pending.resolve(response(session())); await updating;
  assert.equal(h.el('row-menu').hidden, false);
  assert.equal(rowItem(h), item);
});

test('background updates never blur a focused unchanged row when a preceding recent row is inserted', async () => {
  const recent = [seatedRow(1)];
  const h = harness({ recent }); await h.start();
  await h.el('recent-tab').emit('click');
  const first = rowItem(h, 0, 'recent');
  first.children[0].focus();
  recent.unshift(seatedRow(2));
  await h.controller.refresh();
  assert.equal(rowItem(h, 1, 'recent'), first);
  assert.equal(h.doc.activeElement, first.children[0]);
});

test('a failed background fetch cancels an in-flight swipe and cannot reopen a stale menu', async () => {
  let offline = false;
  const h = harness({ fetch: async url => {
    if (offline) throw new TypeError('synthetic connection loss');
    if (url === '/staff/session') return response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  const item = rowItem(h), start = { pointerId: 1, pointerType: 'touch', isPrimary: true, button: 0, clientX: 20, clientY: 20 };
  await item.emit('pointerdown', start);
  offline = true; await h.controller.refresh();
  await item.emit('pointerup', { ...start, clientX: 120 });
  assertHidden(h);
  assert.equal(h.el('row-menu').hidden, true);
});

test('matching view versions cannot conceal different database identities across pages or tabs', async () => {
  for (const split of ['pages', 'tabs']) {
    let inconsistent = false;
    const h = harness({ fetch: async url => {
      if (url === '/staff/session') return response(session());
      const params = new URL(url, 'https://staff.localhost').searchParams, view = params.get('view');
      const other = inconsistent && (split === 'pages' ? params.has('cursor') : view === 'recently_seated');
      const meta = metadata(other ? { database_id: 'other-synthetic-database' } : {});
      if (split === 'pages' && view === 'active' && !params.has('cursor')) {
        return response(listing([reception()], view, { metadata: meta, has_more: true, next_cursor: 'page-two' }));
      }
      return response(listing(view === 'active' ? [reception(2)] : [], view, { metadata: meta }));
    } });
    await h.start(); await rowTrigger(rowItem(h)).emit('click');
    inconsistent = true; await h.controller.refresh();
    assertHidden(h);
    assert.equal(h.el('row-menu').hidden, true);
  }
});

test('dashboard status reserves height and unaffected row and summary styles do not depend on polling state', () => {
  const html = readFileSync(new URL('../../web/staff/index.html', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../web/staff/assets/staff.css', import.meta.url), 'utf8');
  assert.match(html, /class="update-status"[^>]*>\s*<p id="status-message"[^]*?<p id="updated-at"/);
  assert.match(css, /\.update-status\s*\{[^}]*height:\s*44px;[^}]*flex:\s*0 0 44px/);
  assert.match(css, /\.update-status > p\s*\{[^}]*grid-area:\s*1 \/ 1/);
  assert.match(html, /id="update-status"[^]*?id="call-success-view"[^]*?id="call-success-close"[^]*?<div id="staff-view"/);
  assert.match(html, /id="call-success-view"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(css, /\.call-success\s*\{[^}]*grid-area:\s*1 \/ 1;[^}]*height:\s*44px/);
  assert.match(css, /\.call-success > span\s*\{[^}]*max-height:\s*42px;[^}]*overflow:\s*auto/);
  assert.match(css, /scrollbar-gutter:\s*stable/);
  assert.doesNotMatch(css, /data-state=["']?(?:loading|refreshing)[^}]*\}/);
  // CSS checks establish stable layout inputs; actual pixel geometry and touch
  // behaviour still require a real browser and tablet.
});

test('a successful row update rejects old menu nodes and presents only the newer server state', async () => {
  let updated = false;
  const first = reception(), changed = reception(1, { status: 'called', revision: 1,
    called_at: '2026-11-01T18:05:00+09:00', allowed_actions: ['absent', 'seat', 'cancel'] });
  const h = harness({ fetch: async url => {
    if (url === '/staff/session') return response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [updated ? changed : first] : [], view,
      { metadata: metadata({ view_version: (updated ? 'b' : 'a').repeat(64) }) }));
  } });
  await h.start();
  const oldItem = rowItem(h), oldTrigger = rowTrigger(oldItem);
  await oldTrigger.emit('click');
  updated = true;
  await h.controller.refresh();
  assert.equal(h.el('row-menu').hidden, true);
  await oldTrigger.emit('click');
  await swipe(h, oldItem);
  assert.equal(h.el('row-menu').hidden, true);
  await rowTrigger(rowItem(h)).emit('click', { detail: 0 });
  assert.equal(h.el('row-menu-status').textContent, '現在状態：呼出中 · 大人 2名 · 子供 1名');
  assert.match(h.el('row-menu-items').textContent, /不在/);
  assert.doesNotMatch(h.el('row-menu-items').textContent, /呼出（/);
  assertReadOnlyRequests(h.requests);
});

for (const status of [401, 409, 503]) {
  test(`HTTP ${status} during refresh clears the menu and cannot revive it via a tab or stale trigger`, async () => {
    let failure = false;
    const row = reception();
    const h = harness({ fetch: async url => {
      if (url === '/staff/session') return response(session());
      if (failure) return response({}, status);
      const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
      return response(listing(view === 'active' ? [row] : [], view));
    } });
    await h.start();
    const trigger = rowTrigger(rowItem(h));
    await trigger.emit('click');
    failure = true;
    await h.controller.refresh();
    const count = h.requests.length;
    await trigger.emit('click');
    await h.el('recent-tab').emit('click');
    assertHidden(h);
    assert.equal(h.el('row-menu').hidden, true);
    assert.equal(h.requests.length, count);
  });
}

test('logout and local expiry erase all menu target text', async () => {
  for (const method of ['logout', 'expiry']) {
    const h = harness({ rows: [reception()] });
    await h.start();
    await rowTrigger(rowItem(h)).emit('click');
    if (method === 'logout') await h.controller.logout();
    else await h.tick(3600_000);
    assertHidden(h);
    assert.equal(h.el('row-menu').hidden, true);
    assert.equal(h.el('row-menu-title').textContent, '受付メニュー');
    assert.equal(h.el('row-menu-status').textContent, '');
  }
});

test('synthetic badge requires the preview header and is never inferred from receipt data', async () => {
  const plain = harness({ rows: [reception()] });
  await plain.start();
  assert.equal(plain.el('synthetic-preview').hidden, true);
  let isSynthetic = true;
  const marked = harness({ fetch: async url => {
    const result = url === '/staff/session' ? response(session())
      : response(listing([], new URL(url, 'https://staff.localhost').searchParams.get('view')));
    result.headers = { get: name => name === 'X-Omachi-Preview' && isSynthetic ? 'synthetic' : null };
    return result;
  } });
  await marked.start();
  assert.equal(marked.el('synthetic-preview').hidden, false);
  isSynthetic = false;
  await marked.controller.refresh();
  assert.equal(marked.el('synthetic-preview').hidden, true);
});

test('actual HTML connects tabs to unique panels and keeps preview items outside the scrollable lists', () => {
  const html = readFileSync(new URL('../../web/staff/index.html', import.meta.url), 'utf8');
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const prefix of ['active', 'absent', 'recent']) {
    assert.match(html, new RegExp(`id="${prefix}-tab"[^>]*role="tab"[^>]*aria-controls="${prefix}-panel"`));
    assert.match(html, new RegExp(`id="${prefix}-panel"[^>]*role="tabpanel"[^>]*aria-labelledby="${prefix}-tab"`));
  }
  assert.match(html, /id="reception-scroll"[^>]*tabindex="0"/);
  assert.match(html, /閲覧専用プレビュー/);
  assert.doesNotMatch(html, /onclick=|onpointer|<button[^>]*(?:seat|cancel|correction|line-send)/i);
});

test('the only three tab buttons are the upper summary frames with valid content and no duplicate absent counter', () => {
  const html = readFileSync(new URL('../../web/staff/index.html', import.meta.url), 'utf8');
  const top = html.match(/<div class="summary-grid"[^>]*role="tablist"[^>]*>([^]*?)<\/div>/);
  assert.ok(top, 'The summary grid itself must be the tablist');
  assert.equal([...html.matchAll(/role="tablist"/g)].length, 1);
  assert.equal([...html.matchAll(/role="tab"/g)].length, 3);
  const buttons = [...top[1].matchAll(/<button\b([^>]*)>([^]*?)<\/button>/g)];
  assert.equal(buttons.length, 3);
  for (const [index, prefix] of ['active', 'absent', 'recent'].entries()) {
    assert.match(buttons[index][1], new RegExp(`id="${prefix}-tab"`));
    assert.match(buttons[index][1], /type="button"[^>]*role="tab"/);
    assert.match(buttons[index][2], new RegExp(`id="${prefix}-tab-count"`));
    assert.doesNotMatch(buttons[index][2], /<(?:button|input|a|div|p)\b/, 'No nested control or invalid block content');
  }
  assert.match(buttons[0][2], /受付中[^]*?待機<span id="waiting-count">[^]*?呼出<span id="called-count">/);
  assert.match(buttons[1][2], /不在/); assert.match(buttons[2][2], /案内済み[^]*?直近24時間/);
  assert.ok(top.index < html.indexOf('id="reception-scroll"'));
  assert.match(html, /未案内合計[^]*?id="unseated-count"[^]*?不在を含む/);
  assert.doesNotMatch(html, /class="list-tabs"|id="absent-count"/);
  const css = readFileSync(new URL('../../web/staff/assets/staff.css', import.meta.url), 'utf8');
  assert.match(css, /\.summary-grid\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.summary\[aria-selected="true"\]/);
  assert.match(css, /\.tab-count\s*\{[^}]*font-variant-numeric:\s*tabular-nums/);
  assert.doesNotMatch(css, /\.list-tabs|\.summary\.(?:called|absent)|\.summary p/);
});

for (const mode of ['readonly', 'call', 'operations']) {
  test(`${mode} summary tabs reuse sheet navigation and show active breakdown separately from all unseated groups`, async () => {
    const storage = new CallStorage();
    const rows = [reception(1), reception(2, { status: 'called' }), reception(3, { status: 'absent' })];
    const h = harness({ rows, recent: [seatedRow(5), seatedRow(4)], storage,
      callLab: mode === 'call', operationLab: mode === 'operations' }); await h.start();
    assert.equal(h.el('active-tab-count').textContent, '2');
    assert.equal(h.el('waiting-count').textContent, '1'); assert.equal(h.el('called-count').textContent, '1');
    assert.equal(h.el('absent-tab-count').textContent, '1'); assert.equal(h.el('recent-tab-count').textContent, '2');
    assert.equal(h.el('unseated-count').textContent, '3');
    const journal = [...storage.values.entries()], requestCount = h.requests.length;
    for (const prefix of ['absent', 'recent', 'active']) {
      // Keyboard-generated button activation has detail=0; actual Enter/Space
      // default activation is provided by the native HTML button, not new JS.
      await h.el(`${prefix}-tab`).emit('click', { detail: 0 });
      assert.equal(h.el(`${prefix}-panel`).hidden, false);
      assert.equal(h.el(`${prefix}-tab`).getAttribute('aria-selected'), 'true');
      assert.equal(h.el(`${prefix}-tab`).getAttribute('tabindex'), '0');
    }
    assert.equal(h.requests.length, requestCount); assert.deepEqual([...storage.values.entries()], journal);
    assertReadOnlyRequests(h.requests); h.controller.stop();
  });
}

test('focused summary frames and the selected sheet survive polling and digit changes without rebuilding list rows', async () => {
  const rows = [reception(1), reception(2, { status: 'absent' })];
  const h = harness({ rows }); await h.start(); await h.el('absent-tab').emit('click');
  const absent = rowItem(h, 0, 'absent'), frame = h.el('absent-tab'); frame.focus();
  h.el('reception-scroll').scrollTop = 180;
  for (let i = 3; i <= 12; i++) rows.push(reception(i));
  await h.tick(5001);
  assert.equal(h.el('active-tab-count').textContent, '11'); assert.equal(h.el('waiting-count').textContent, '11');
  assert.equal(h.el('unseated-count').textContent, '12'); assert.equal(h.el('absent-tab-count').textContent, '1');
  for (let i = 0; i < 20; i++) {
    await h.tick(5001);
    assert.equal(h.doc.activeElement, frame); assert.equal(h.el('absent-tab'), frame);
    assert.equal(frame.getAttribute('aria-selected'), 'true'); assert.equal(rowItem(h, 0, 'absent'), absent);
    assert.equal(h.el('reception-scroll').scrollTop, 180);
  }
  assertReadOnlyRequests(h.requests); h.controller.stop();
});

test('login communication loss has no automatic credential resubmission', async () => {
  const h = harness({ page: 'login', fetch: async (_url, options) => {
    if (options.method === 'POST') throw new TypeError('network outage');
    return response({}, 401);
  } });
  await h.start();
  h.el('staff-token').value = 'synthetic-credential';
  await h.controller.login();
  await h.tick(15_000);
  assertHidden(h);
  assert.equal(h.requests.filter(item => item.method === 'POST').length, 1);
  assert.equal(h.el('staff-token').value, '');
});

for (const invalid of [null, {}, { authenticated: false }, session({ csrf_token: '' }), session({ expires_in_seconds: 0 })]) {
  test(`invalid session response fails closed (${JSON.stringify(invalid)})`, async () => {
    const h = harness({ fetch: async () => response(invalid) });
    await h.start();
    assertHidden(h);
    assert.equal(h.requests.filter(item => item.url.startsWith('/staff/receptions')).length, 0);
  });
}

test('logout uses the in-memory CSRF value and clears reception details before the server replies', async () => {
  const pending = deferred();
  const row = reception();
  const h = harness({ fetch: async (url, options) => {
    if (url === '/staff/session') return options.method === 'DELETE' ? pending.promise : response(session());
    if (url.endsWith('/operation-context')) return response({ metadata: metadata(), reception: row });
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [row] : [], view));
  } });
  await h.start();
  await h.controller.openDetail(row.id);
  const loggingOut = h.controller.logout();
  await settle();
  assertHidden(h);
  const request = h.requests.find(item => item.method === 'DELETE');
  assert.equal(request.url, '/staff/session');
  assert.equal(request.headers['X-CSRF-Token'], session().csrf_token);
  assert.equal(request.headers['X-Staff-Token'], undefined);
  pending.resolve(response({ authenticated: false }));
  await loggingOut;
  assert.ok(h.redirects.some(path => /login/.test(path)));
  assertReadOnlyRequests(h.requests);
});

test('an unconfirmed logout cannot revive confidential information through refresh or background polling', async () => {
  let deletes = 0;
  const row = reception();
  const h = harness({ fetch: async (url, options) => {
    if (url === '/staff/session') {
      if (options.method === 'DELETE') { deletes += 1; throw new TypeError('reply lost'); }
      return response(session());
    }
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [row] : [], view));
  } });
  await h.start();
  await h.controller.logout();
  const requestsAfterLogout = h.requests.length;
  await h.controller.refresh();
  await h.tick(10_000);
  assertHidden(h);
  assert.equal(h.requests.length, requestsAfterLogout);
  assert.equal(deletes, 1);
  assert.match(h.el('status-message').textContent + h.el('login-message').textContent, /ログアウト|確認|通信/);
});

test('logout rejection by CSRF cannot leave staff data visible', async () => {
  const h = harness({ fetch: async (url, options) => {
    if (url === '/staff/session') return options.method === 'DELETE' ? response({}, 403) : response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  await h.controller.logout();
  assertHidden(h);
  assert.equal(h.requests.filter(item => item.method === 'DELETE').length, 1);
});

for (const status of [401, 403]) {
  test(`logout HTTP ${status} followed by a valid session remains hidden and refreshes CSRF only for an explicit retry`, async () => {
    let deletes = 0;
    const renewedCsrf = 'd'.repeat(43);
    const h = harness({ fetch: async (url, options) => {
      if (url === '/staff/session') {
        if (options.method === 'DELETE') {
          deletes += 1;
          return deletes === 1 ? response({}, status) : response({ authenticated: false });
        }
        return response(session(deletes ? { csrf_token: renewedCsrf } : {}));
      }
      const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
      return response(listing(view === 'active' ? [reception()] : [], view));
    } });
    await h.start();
    await h.controller.logout();
    assertHidden(h);
    assert.deepEqual(h.redirects, []);
    assert.equal(h.requests.at(-1).url, '/staff/session');
    assert.equal(h.requests.at(-1).method, 'GET');
    assert.match(h.el('status-message').textContent, /確認できません|未確認|再試行/);
    const confirmationRequestCount = h.requests.length;
    await h.controller.refresh();
    await h.tick(1000);
    assert.equal(h.requests.length, confirmationRequestCount);
    await h.controller.logout();
    const deleteRequests = h.requests.filter(item => item.method === 'DELETE');
    assert.equal(deleteRequests.length, 2);
    assert.equal(deleteRequests[0].headers['X-CSRF-Token'], session().csrf_token);
    assert.equal(deleteRequests[1].headers['X-CSRF-Token'], renewedCsrf);
    assert.ok(h.redirects.some(path => /login/.test(path)));
    assertHidden(h);
    assertReadOnlyRequests(h.requests);
  });
}

test('retrying a successfully invalidated logout after response loss confirms expiry without restoring the UI', async () => {
  let invalidated = false;
  const h = harness({ fetch: async (url, options) => {
    if (url === '/staff/session') {
      if (options.method === 'DELETE') {
        if (invalidated) return response({}, 401);
        invalidated = true;
        throw new TypeError('successful logout response was lost');
      }
      return invalidated ? response({}, 401) : response(session());
    }
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  await h.controller.logout();
  assertHidden(h);
  assert.deepEqual(h.redirects, []);
  await h.controller.logout();
  assertHidden(h);
  assert.equal(h.requests.filter(item => item.method === 'DELETE').length, 2);
  assert.equal(h.requests.at(-1).url, '/staff/session');
  assert.equal(h.requests.at(-1).method, 'GET');
  assert.ok(h.redirects.some(path => /login/.test(path)));
  assert.match(h.el('status-message').textContent, /ログアウトしました|失効/);
  assertReadOnlyRequests(h.requests);
});

test('failed logout confirmation cannot be mistaken for a verified session invalidation', async () => {
  let loggingOut = false;
  const h = harness({ fetch: async (url, options) => {
    if (url === '/staff/session') {
      if (options.method === 'DELETE') { loggingOut = true; return response({}, 403); }
      if (loggingOut) throw new TypeError('confirmation network failure');
      return response(session());
    }
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing([], view));
  } });
  await h.start();
  await h.controller.logout();
  assertHidden(h);
  assert.deepEqual(h.redirects, []);
  assert.match(h.el('status-message').textContent, /確認できません|未確認|再試行/);
});

test('HTTP 403 from logout confirmation proves no session invalidation and keeps the logout pending', async () => {
  let loggingOut = false;
  const h = harness({ fetch: async (url, options) => {
    if (url === '/staff/session') {
      if (options.method === 'DELETE') { loggingOut = true; return response({}, 403); }
      return loggingOut ? response({}, 403) : response(session());
    }
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing([], view));
  } });
  await h.start();
  await h.controller.logout();
  assertHidden(h);
  assert.deepEqual(h.redirects, []);
  assert.match(h.el('status-message').textContent, /確認できません|未確認|再試行/);
  assert.equal(h.requests.at(-1).method, 'GET');
});

test('polling refreshes only after five seconds and reflects external state changes', async () => {
  let state = 'waiting';
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception(1, { status: state })] : [], view));
  } });
  await h.start();
  const initial = h.requests.length;
  await h.tick(4999);
  assert.equal(h.requests.length, initial);
  state = 'called';
  await h.tick(1);
  assert.ok(h.requests.length > initial);
  assert.equal(h.el('waiting-count').textContent, '0');
  assert.equal(h.el('called-count').textContent, '1');
  assertReadOnlyRequests(h.requests);
});

test('background polling does not overlap an unfinished refresh', async () => {
  const pending = deferred();
  let hold = false;
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    if (view === 'active' && hold) return pending.promise;
    return response(listing([], view));
  } });
  await h.start();
  hold = true;
  const refreshing = h.controller.refresh();
  await settle();
  const inFlight = h.requests.length;
  await h.tick(10_000);
  assert.equal(h.requests.length, inFlight);
  pending.resolve(response(listing()));
  await refreshing;
  assert.equal(h.el('staff-view').hidden, false);
});

for (const phase of ['fetch', 'body']) {
  test(`the fifteen-second ${phase} timeout aborts a stalled list request and clears current counts`, async () => {
    const h = harness({ fetch: async (url) => {
      if (url === '/staff/session') return response(session());
      if (phase === 'fetch') return new Promise(() => {});
      return { ok: true, status: 200, json() { return new Promise(() => {}); } };
    } });
    const starting = h.start();
    await settle();
    const request = h.requests.find(item => item.url.startsWith('/staff/receptions'));
    assert.ok(request);
    await h.tick(14999);
    assert.equal(request.signal.aborted, false);
    await h.tick(1);
    await starting;
    assert.equal(request.signal.aborted, true);
    assert.equal(h.el('active-list').textContent, '');
    assert.notEqual(h.el('waiting-count').textContent, '0');
    assert.notEqual(h.el('status-message').textContent, '');
  });
}

test('session expiry detected during refresh hides all prior rows and stops polling', async () => {
  let expired = false;
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return expired ? response({}, 401) : response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  expired = true;
  await h.controller.refresh();
  assertHidden(h);
  const stoppedAt = h.requests.length;
  await h.tick(15_000);
  assert.equal(h.requests.length, stoppedAt);
});

test('session confirmation communication failure hides previously visible staff data', async () => {
  let offline = false;
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') {
      if (offline) throw new TypeError('no network');
      return response(session());
    }
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  offline = true;
  await h.controller.refresh();
  assertHidden(h);
});

test('every active page contributes to counts, with no false fifty-row total', async () => {
  const rows = Array.from({ length: 151 }, (_, index) => reception(index + 1, {
    status: index < 110 ? 'waiting' : index < 140 ? 'called' : 'absent',
  }));
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const query = new URL(url, 'https://staff.localhost').searchParams;
    const view = query.get('view');
    if (view !== 'active') return response(listing([], view));
    assert.equal(query.get('limit'), '100');
    const next = query.get('cursor');
    return next === null ? response(listing(rows.slice(0, 100), view, { has_more: true, next_cursor: 'synthetic-page-2' }))
      : response(listing(rows.slice(100), view));
  } });
  await h.start();
  assert.equal(h.el('waiting-count').textContent, '110');
  assert.equal(h.el('called-count').textContent, '30');
  assert.equal(h.el('absent-tab-count').textContent, '11');
  assert.match(h.el('absent-list').textContent, /151/);
  assert.equal(h.el('unseated-count').textContent, '151');
  assertReadOnlyRequests(h.requests);
});

test('recent seating pages are read with the bounded 24-hour window and preserve current seating order', async () => {
  const seated = index => reception(index, {
    status: 'seated', seated_at: '2026-11-01T18:25:00+09:00', actual_wait_minutes: 25, revision: 1,
    allowed_actions: [], valid_seating_event: { event_id: index + 1000, from_status: 'waiting', to_status: 'seated', occurred_at: '2026-11-01T18:25:00+09:00', correctable: false },
  });
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const query = new URL(url, 'https://staff.localhost').searchParams;
    if (query.get('view') === 'active') return response(listing());
    assert.equal(query.get('hours'), '24');
    return query.has('cursor') ? response(listing([seated(1)], 'recently_seated'))
      : response(listing([seated(2)], 'recently_seated', { has_more: true, next_cursor: 'recent-second-page' }));
  } });
  await h.start();
  const text = h.el('recent-list').textContent;
  assert.ok(text.includes('002') && text.includes('001'));
  assert.ok(text.indexOf('002') < text.indexOf('001'));
});

for (const status of [409, 503]) {
  test(`HTTP ${status} midway through paging clears the partial results and counts`, async () => {
    const h = harness({ fetch: async (url) => {
      if (url === '/staff/session') return response(session());
      const query = new URL(url, 'https://staff.localhost').searchParams;
      if (query.get('view') !== 'active') return response(listing([], 'recently_seated'));
      return query.has('cursor') ? response({}, status)
        : response(listing([reception()], 'active', { has_more: true, next_cursor: 'stale-page' }));
    } });
    await h.start();
    assert.equal(h.el('active-list').textContent, '');
    assert.equal(h.el('recent-list').textContent, '');
    assert.notEqual(h.el('waiting-count').textContent, '1');
    assert.notEqual(h.el('status-message').textContent, '');
    assert.equal(h.requests.some(item => item.method === 'POST'), false);
  });
}

test('active and recent views from different event versions cannot be published as one current screen', async () => {
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view,
      { metadata: metadata({ view_version: (view === 'active' ? 'a' : 'b').repeat(64) }) }));
  } });
  await h.start();
  assert.equal(h.el('active-list').textContent, '');
  assert.notEqual(h.el('waiting-count').textContent, '1');
  assert.notEqual(h.el('status-message').textContent, '');
});

test('the same reception cannot appear in both the active and seated views of one snapshot', async () => {
  const seated = reception(1, { status: 'seated', seated_at: '2026-11-01T18:25:00+09:00',
    actual_wait_minutes: 25, revision: 1, allowed_actions: [],
    valid_seating_event: { event_id: 10, from_status: 'waiting', to_status: 'seated',
      occurred_at: '2026-11-01T18:25:00+09:00', correctable: false } });
  const h = harness({ rows: [reception()], recent: [seated] });
  await h.start();
  assert.equal(h.el('active-list').textContent, '');
  assert.equal(h.el('recent-list').textContent, '');
  assert.notEqual(h.el('waiting-count').textContent, '1');
  assert.notEqual(h.el('status-message').textContent, '');
});

test('a repeated paging cursor is rejected rather than looping or publishing partial counts', async () => {
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const query = new URL(url, 'https://staff.localhost').searchParams;
    return query.get('view') === 'active'
      ? response(listing([reception(query.has('cursor') ? 2 : 1)], 'active', { has_more: true, next_cursor: 'repeated-cursor' }))
      : response(listing([], 'recently_seated'));
  } });
  await h.start();
  assert.ok(h.requests.length < 10);
  assert.equal(h.el('active-list').textContent, '');
  assert.notEqual(h.el('waiting-count').textContent, '2');
});

test('the one-hundred-page safety limit never publishes an incomplete global count', async () => {
  let pages = 0;
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const query = new URL(url, 'https://staff.localhost').searchParams;
    assert.equal(query.get('view'), 'active');
    pages += 1;
    return response(listing([reception(pages)], 'active', { has_more: true, next_cursor: `synthetic-page-${pages + 1}` }));
  } });
  await h.start();
  assert.equal(pages, 100);
  assert.equal(h.el('active-list').textContent, '');
  assert.notEqual(h.el('waiting-count').textContent, '100');
  assert.match(h.el('status-message').textContent, /全件|組数/);
});

test('duplicate reception identities across pages fail closed', async () => {
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const query = new URL(url, 'https://staff.localhost').searchParams;
    if (query.get('view') !== 'active') return response(listing([], 'recently_seated'));
    return response(listing([reception()], 'active', query.has('cursor') ? {} : { has_more: true, next_cursor: 'second-page' }));
  } });
  await h.start();
  assert.equal(h.el('active-list').textContent, '');
  assert.notEqual(h.el('waiting-count').textContent, '2');
});

test('a response from a superseded refresh cannot replace newer reception information', async () => {
  let pendingRefresh = false;
  const old = deferred();
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    if (view === 'active' && pendingRefresh) { pendingRefresh = false; return old.promise; }
    return response(listing(view === 'active' ? [reception(2)] : [], view));
  } });
  await h.start();
  pendingRefresh = true;
  const first = h.controller.refresh();
  await settle();
  await h.controller.refresh();
  old.resolve(response(listing([reception(1)])));
  await first;
  assert.match(h.el('active-list').textContent, /002/);
  assert.doesNotMatch(h.el('active-list').textContent, /001/);
});

test('an in-flight reception response cannot repopulate the screen after logout', async () => {
  const old = deferred();
  let hold = false;
  const h = harness({ fetch: async (url, options) => {
    if (url === '/staff/session') return response(options.method === 'DELETE' ? { authenticated: false } : session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    if (view === 'active' && hold) return old.promise;
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  hold = true;
  const refreshing = h.controller.refresh();
  await settle();
  await h.controller.logout();
  old.resolve(response(listing([reception()])));
  await refreshing;
  assertHidden(h);
});

test('opening a reception uses only the existing read-only operation-context API', async () => {
  const row = reception();
  const h = harness({ rows: [row] });
  await h.start();
  await h.controller.openDetail(row.id);
  assert.equal(h.el('detail-view').hidden, false);
  assert.match(h.el('detail-title').textContent + h.el('detail-content').textContent, /001/);
  assert.ok(h.requests.some(item => item.url === `/staff/receptions/${row.id}/operation-context`));
  assertReadOnlyRequests(h.requests);
  h.controller.closeDetail();
  assert.equal(h.el('detail-view').hidden, true);
});

for (const kind of ['wrong-id', 'different-version', 'not-found', 'unauthorized']) {
  test(`detail ${kind} does not display an unverified or stale target`, async () => {
    const row = reception();
    const h = harness({ fetch: async (url) => {
      if (url === '/staff/session') return response(session());
      if (url.endsWith('/operation-context')) {
        if (kind === 'not-found') return response({}, 404);
        if (kind === 'unauthorized') return response({}, 401);
        return response({ metadata: metadata(kind === 'different-version' ? { view_version: 'b'.repeat(64) } : {}),
          reception: kind === 'wrong-id' ? reception(2) : row });
      }
      const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
      return response(listing(view === 'active' ? [row] : [], view));
    } });
    await h.start();
    await h.controller.openDetail(row.id);
    assert.equal(h.el('detail-content').textContent, '');
    assert.equal(h.el('detail-view').hidden, true);
    if (kind === 'unauthorized') assertHidden(h);
  });
}

test('a delayed detail response cannot replace a more recently selected reception', async () => {
  const pending = deferred();
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    if (url.endsWith('/operation-context')) {
      if (url.includes(reception(1).id)) return pending.promise;
      return response({ metadata: metadata(), reception: reception(2) });
    }
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception(1), reception(2)] : [], view));
  } });
  await h.start();
  const old = h.controller.openDetail(reception(1).id);
  await settle();
  await h.controller.openDetail(reception(2).id);
  pending.resolve(response({ metadata: metadata(), reception: reception(1) }));
  await old;
  const text = h.el('detail-title').textContent + h.el('detail-content').textContent;
  assert.match(text, /002/);
  assert.doesNotMatch(text, /001/);
});

test('reception text is rendered as inert text rather than executed HTML', async () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const row = reception(1, { display_number: hostile });
  const h = harness({ rows: [row] });
  await h.start();
  // A strict response validator may instead reject the malformed number. Both
  // paths are safe; the harness throws for every attempted innerHTML write.
  if (!h.el('active-list').textContent.includes(hostile)) {
    assert.equal(h.el('active-list').textContent, '');
    assert.notEqual(h.el('status-message').textContent, '');
  } else {
    await h.controller.openDetail(row.id);
    assert.match(h.el('detail-title').textContent + h.el('detail-content').textContent, /<img/);
  }
  assertReadOnlyRequests(h.requests);
});

for (const status of ['unknown', 'cancelled', 'seated']) {
  test(`unexpected active status ${status} cannot contribute to a misleading count`, async () => {
    const h = harness({ rows: [reception(1, { status })] });
    await h.start();
    assert.equal(h.el('active-list').textContent, '');
    assert.notEqual(h.el('waiting-count').textContent, '1');
    assert.notEqual(h.el('status-message').textContent, '');
  });
}

test('pagehide removes confidential DOM before a document can enter the browser back-forward cache', async () => {
  const h = harness({ rows: [reception()] });
  await h.start();
  await h.env.emit('pagehide', { persisted: true });
  assertHidden(h);
});

test('a restored back-forward-cache page must authenticate again before showing its rows', async () => {
  let expired = false;
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return expired ? response({}, 401) : response(session());
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  await h.env.emit('pagehide', { persisted: true });
  expired = true;
  const before = h.requests.length;
  await h.env.emit('pageshow', { persisted: true });
  assertHidden(h);
  assert.equal(h.requests.length, before + 1);
  assert.equal(h.requests.at(-1).url, '/staff/session');
});

test('a hidden tab clears information and pauses polling until a fresh session check on visibility', async () => {
  const h = harness({ rows: [reception()] });
  await h.start();
  h.doc.visibilityState = 'hidden';
  h.doc.hidden = true;
  await h.doc.emit('visibilitychange');
  assertHidden(h);
  const paused = h.requests.length;
  await h.tick(15_000);
  assert.equal(h.requests.length, paused);
  h.doc.visibilityState = 'visible';
  h.doc.hidden = false;
  await h.doc.emit('visibilitychange');
  assert.equal(h.requests[paused].url, '/staff/session');
  assert.equal(h.el('staff-view').hidden, false);
  assert.equal(h.el('waiting-count').textContent, '1');
});

test('a session that expires while an old list request is in flight cannot publish that list', async () => {
  const old = deferred();
  let hold = false;
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session({ expires_in_seconds: 1 }));
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    if (view === 'active' && hold) return old.promise;
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  hold = true;
  const refreshing = h.controller.refresh();
  await settle();
  await h.tick(1000);
  old.resolve(response(listing([reception()])));
  await refreshing;
  assertHidden(h);
});

test('a detail survives an unrelated global version change but closes when its target revision changes', async () => {
  let version = 'a'.repeat(64);
  let row = reception();
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const current = metadata({ view_version: version });
    if (url.endsWith('/operation-context')) return response({ metadata: current, reception: row });
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [row] : [], view, { metadata: current }));
  } });
  await h.start();
  await h.controller.openDetail(reception().id);
  assert.equal(h.el('detail-view').hidden, false);
  version = 'b'.repeat(64);
  await h.controller.refresh();
  assert.equal(h.el('detail-view').hidden, false);
  row = reception(1, { revision: 1, status: 'called' });
  version = 'c'.repeat(64);
  await h.controller.refresh();
  assert.equal(h.el('detail-view').hidden, true);
  assert.equal(h.el('detail-content').textContent, '');
});

test('a list communication error clears old counts instead of presenting them as current', async () => {
  let failed = false;
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    if (failed) throw new TypeError('connection reset');
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view));
  } });
  await h.start();
  failed = true;
  await h.controller.refresh();
  assert.equal(h.el('active-list').textContent, '');
  assert.equal(h.el('recent-list').textContent, '');
  assert.notEqual(h.el('waiting-count').textContent, '1');
  assert.notEqual(h.el('status-message').textContent, '');
});

test('v1 legacy schema remains readable without exposing a writable operation control', async () => {
  const legacy = metadata({ schema_version: 1, database_id: null, write_epoch: null,
    staff_write_mode: 'legacy', write_enabled: false, disabled_reason: 'protected_mode_required' });
  const row = reception(1, { revision: null, allowed_actions: [] });
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    if (url.endsWith('/operation-context')) return response({ metadata: legacy, reception: row });
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [row] : [], view, { metadata: legacy }));
  } });
  await h.start();
  assert.equal(h.el('waiting-count').textContent, '1');
  await h.controller.openDetail(row.id);
  assert.equal(h.el('detail-view').hidden, false);
  assertReadOnlyRequests(h.requests);
});

test('stopping the controller clears staff information and cancels subsequent polling', async () => {
  const h = harness({ rows: [reception()] });
  await h.start();
  h.controller.stop();
  assertHidden(h);
  const count = h.requests.length;
  await h.tick(20_000);
  assert.equal(h.requests.length, count);
});

for (const [protocol, hostname] of [['http:', 'staff.localhost'], ['https:', 'localhost'], ['https:', 'staff.localhost.evil']]) {
  test(`browser guard rejects ${protocol}//${hostname} with no HTTP fallback`, async () => {
    const h = harness();
    h.env.location.protocol = protocol;
    h.env.location.hostname = hostname;
    await h.start();
    assertHidden(h);
    assert.equal(h.requests.length, 0);
    assert.equal(h.redirects.some(path => path.startsWith('http:')), false);
  });
}

for (const page of ['login', 'receptions']) {
  test(`a rejected origin cannot issue requests through direct ${page} controller calls`, async () => {
    const h = harness({ page });
    h.env.location.protocol = 'http:';
    await h.start();
    h.el('staff-token').value = 'synthetic-credential-must-not-cross-insecure-http';
    await h.controller.login();
    await h.controller.refresh();
    assertHidden(h);
    assert.equal(h.requests.length, 0);
    assert.equal(h.redirects.some(path => path.startsWith('http:')), false);
  });
}

// 8445 adapter tests exercise the real shared foundation with a synthetic
// transport ledger. No browser cookies, credentials or real database are used.
function operationHarness(options = {}) {
  const rows = options.rows ?? [reception(1, options.row ?? {})];
  const storage = options.storage ?? new CallStorage(), ledger = options.ledger ?? new Map();
  let contexts = 0;
  const h = harness({ ...options, rows, storage, operationLab: true, callLab: false, fetch: async (url, request) => {
    if (url.endsWith('/operation-context')) {
      contexts++;
      const row = rows.find(r => url === `/staff/receptions/${r.id}/operation-context`);
      const override = await options.context?.(row, contexts, url, request);
      return override ?? response({ metadata: metadata(options.meta), reception: { ...row } });
    }
    if (url.startsWith('/staff/operations/')) {
      const override = await options.lookup?.(url, request);
      if (override !== undefined) return override;
      const found = ledger.get(url.split('/').at(-1)); return response(found ?? {}, found ? 200 : 404);
    }
    if (url.endsWith('/operations') && request.method === 'POST') {
      const body = JSON.parse(request.body);
      assert.ok(options.seatLab ? body.action === 'seat' : ['call', 'absent'].includes(body.action));
      assert.deepEqual(JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY)).request, body);
      assert.equal(request.headers['X-CSRF-Token'], session().csrf_token);
      assert.equal(request.headers['X-Staff-Token'], undefined);
      const apply = () => {
        if (!ledger.has(body.operation_id)) {
          if (body.action === 'seat') {
            const index = rows.findIndex(r => url === `/staff/receptions/${r.id}/operations`), before = rows[index];
            const seated = seatedRow(before.reception_order);
            seated.valid_seating_event.from_status = before.status;
            rows[index] = { ...before, status: 'seated', revision: body.expected_revision + 1, called_at: before.called_at,
              seated_at: seated.seated_at, actual_wait_minutes: seated.actual_wait_minutes,
              valid_seating_event: seated.valid_seating_event, allowed_actions: [] };
            ledger.set(body.operation_id, { ...callReceipt(rows[index], body), action: 'seat', recorded_at: seated.seated_at });
          } else {
            const index = rows.findIndex(r => url === `/staff/receptions/${r.id}/operations`);
            rows[index] = { ...rows[index], revision: body.expected_revision + 1, status: body.action === 'call' ? 'called' : 'absent',
              called_at: body.action === 'call' ? '2026-11-01T18:10:00+09:00' : rows[index].called_at,
              allowed_actions: body.action === 'call' ? ['absent', 'seat', 'cancel'] : ['call', 'return', 'seat', 'cancel'] };
            ledger.set(body.operation_id, { ...callReceipt(rows[index], body), action: body.action });
          }
        }
        return response(ledger.get(body.operation_id));
      };
      const override = await options.post?.(body, request, rows, ledger, apply);
      return override ?? apply();
    }
    const override = await options.fetch?.(url, request);
    if (override === undefined && options.seatLab && url.startsWith('/staff/receptions?')) {
      const view = new URL(url, 'https://staff.localhost:8446').searchParams.get('view');
      const visible = rows.filter(r => view === 'active' ? ['waiting', 'called', 'absent'].includes(r.status) : r.status === 'seated');
      if (view === 'recently_seated') visible.sort((a, b) => b.valid_seating_event.event_id - a.valid_seating_event.event_id);
      return response(listing(visible, view));
    }
    return override;
  } });
  const posts = () => h.requests.filter(r => r.method === 'POST' && r.url.endsWith('/operations'));
  return { ...h, rows, storage, ledger, posts,
    async openMenu(action = options.seatLab ? 'seat' : 'call', index = 0) {
      const view = rows[index].status === 'absent' ? 'absent' : 'active';
      await h.el(`${view}-tab`).emit('click');
      await rowTrigger(rowItem(h, index, view)).emit('click');
      const button = h.el('row-menu-items').children.flatMap(item => item.children).find(item => item.textContent === (action === 'seat' ? '案内完了' : action === 'call' ? '呼出' : '不在'));
      assert.ok(button, `Expected ${action} action item`); this.actionButton = button;
    },
    async chooseAction() { await this.actionButton?.emit('click'); await settle(); await settle(); },
  };
}

test('8445 waiting→called→absent→called uses one shared journal and preserves intake and called time', async () => {
  const h = operationHarness(); await h.start();
  const original = { ...h.rows[0] };
  for (const [action, status, word] of [['call', 'called', '呼出'], ['absent', 'absent', '不在'], ['call', 'called', '呼出']]) {
    const previous = h.posts().length;
    await h.openMenu(action);
    const selectedSheet = h.rows[0].status === 'absent' ? 'absent' : 'active';
    assert.equal(h.posts().length, previous);
    assert.equal(h.el('row-menu').hidden, false);
    assert.equal(h.actionButton.textContent, word);
    const called = h.rows[0].called_at;
    await h.chooseAction();
    assert.equal(h.rows[0].status, status);
    assert.equal(h.el(`${selectedSheet}-tab`).getAttribute('aria-selected'), 'true', 'writes cannot switch the selected sheet');
    const targetSheet = status === 'absent' ? 'absent' : 'active';
    assert.equal(rowItem(h, 0, targetSheet).dataset.receptionId, original.id);
    assert.equal(h.el(`${targetSheet === 'active' ? 'absent' : 'active'}-list`).children.length, 0);
    assert.equal(h.el('unseated-count').textContent, '1');
    assert.equal(h.posts().length, previous + 1);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null);
    assert.match(h.el('call-success-message').textContent, new RegExp(`${word}成功.*最新一覧`));
    if (action === 'absent') assert.equal(h.rows[0].called_at, called);
    for (const field of ['id', 'display_number', 'reception_order', 'received_at', 'adults', 'children', 'total_people']) assert.equal(h.rows[0][field], original[field]);
  }
  const receipts = [...h.ledger.values()];
  assert.deepEqual(receipts[0].reception.snapshot, receipts[1].reception.snapshot);
  assert.deepEqual(receipts[0].reception.prediction, receipts[1].reception.prediction);
  assert.equal(new Set(h.posts().map(p => JSON.parse(p.body).operation_id)).size, 3);
  h.controller.stop();
});

for (const [action, status] of [['call', 'waiting'], ['absent', 'called']]) {
  test(`8445 ${action} double-click sends once and its confirmed toast expires without changing rows/focus/scroll`, async () => {
    const h = operationHarness({ row: { status, called_at: status === 'called' ? '2026-11-01T18:05:00+09:00' : null,
      allowed_actions: [action] } }); await h.start(); await h.openMenu(action);
    await Promise.all([h.chooseAction(), h.chooseAction()]);
    assert.equal(h.posts().length, 1);
    const view = action === 'absent' ? 'absent' : 'active';
    await h.el(`${view}-tab`).emit('click');
    const row = rowItem(h, 0, view); await rowTrigger(row).emit('click'); const focus = h.doc.activeElement;
    h.el('reception-scroll').scrollTop = 150;
    await h.tick(5001);
    assert.equal(h.el('call-success-view').hidden, true);
    assert.equal(rowItem(h, 0, view), row); assert.equal(h.el('row-menu').hidden, false);
    assert.equal(h.doc.activeElement, focus); assert.equal(h.el('reception-scroll').scrollTop, 150);
    assert.equal(h.posts().length, 1); h.controller.stop();
  });
}

for (const change of ['status', 'revision', 'write_epoch', 'database_id', 'allowed_actions']) {
  test(`8445 final context ${change} change invalidates the selected operation before any POST`, async () => {
    const h = operationHarness({ context: (row, n) => {
      if (n !== 2) return;
      const value = { reception: { ...row }, metadata: metadata() };
      if (change === 'status') value.reception.status = 'absent';
      if (change === 'revision') value.reception.revision++;
      if (change === 'allowed_actions') value.reception.allowed_actions = [];
      if (['database_id', 'write_epoch'].includes(change)) value.metadata[change] = randomUUID();
      return response(value);
    } });
    await h.start(); await h.openMenu(); await h.chooseAction();
    assert.equal(h.posts().length, 0);
    assert.match(h.el('call-status-message').textContent, /確認|選び直/); h.controller.stop();
  });
}

test('8445 changed source at the first context prevents sending', async () => {
  const h = operationHarness({ context: row => response({ metadata: metadata(), reception: { ...row, status: 'called',
    revision: 1, called_at: '2026-11-01T18:10:00+09:00', allowed_actions: ['absent'] } }) });
  await h.start(); await h.openMenu(); await h.chooseAction();
  assert.equal(h.posts().length, 0); h.controller.stop();
});

test('8445 menu and focus survive unchanged polling, but changing the target closes it', async () => {
  const h = operationHarness(); await h.start(); await h.openMenu();
  const focus = h.doc.activeElement;
  for (let i = 0; i < 25; i++) await h.tick(5000);
  assert.equal(h.doc.activeElement, focus);
  h.rows[0] = { ...h.rows[0], revision: 1 }; await h.controller.refresh();
  await h.chooseAction(); assert.equal(h.posts().length, 0); h.controller.stop();
});

for (const action of ['call', 'absent']) {
  test(`8445 lost ${action} response blocks both actions, 404 keeps ID and explicit retry keeps exact body`, async () => {
    let first = true, missing = true;
    const h = operationHarness({ row: action === 'absent' ? { status: 'called', called_at: '2026-11-01T18:05:00+09:00', allowed_actions: ['absent'] } : {},
      post: (_, __, ___, ____, apply) => { if (first) { first = false; apply(); throw Error('synthetic response loss'); } },
      lookup: () => missing ? response({}, 404) : undefined });
    await h.start(); await h.openMenu(action); await h.chooseAction();
    assert.match(h.el('call-status-message').textContent, /結果不明/);
    const saved = h.storage.getItem(OPERATION_JOURNAL_KEY), body = h.posts()[0].body;
    await h.el('call-lookup').emit('click');
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), saved);
    await h.tick(5001); assert.equal(h.el('call-status-view').hidden, false);
    const view = action === 'absent' ? 'absent' : 'active';
    await h.el(`${view}-tab`).emit('click');
    await rowTrigger(rowItem(h, 0, view)).emit('click');
    assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0);
    await h.el('call-retry').emit('click'); await settle(); await settle();
    assert.equal(h.posts().length, 2); assert.equal(h.posts()[1].body, body);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null);
    missing = false; h.controller.stop();
  });
}

for (const seatLab of [false, true]) test(`${seatLab ? 8446 : 8445} persisted rejection never auto-hides or changes reception state`, async () => {
  const h = operationHarness({ seatLab, post: (body, _, rows, ledger) => {
    const receipt = { operation_id: body.operation_id, reception_id: rows[0].id, action: body.action,
      recorded_at: '2026-11-01T18:10:00+09:00', resulting_revision: 0, reception: null,
      outcome: 'rejected', response_status: 409, error: { code: 'revision_conflict', detail: 'synthetic conflict' } };
    ledger.set(body.operation_id, receipt); return response(receipt, 409);
  } });
  await h.start(); await h.openMenu(); await h.chooseAction();
  assert.equal(h.rows[0].status, 'waiting'); assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null);
  assert.match(h.el('call-status-message').textContent, /拒否・競合/);
  await h.tick(10001); assert.equal(h.el('call-status-view').hidden, false); assert.equal(h.el('call-success-view').hidden, true);
  h.controller.stop();
});

test('8445 result success without a verified latest list never becomes an expiring toast', async () => {
  let fail = false;
  const h = operationHarness({ post: (_, __, ___, ____, apply) => { const result = apply(); fail = true; return result; },
    fetch: url => fail && url.startsWith('/staff/receptions?') ? response({}, 503) : undefined });
  await h.start(); await h.openMenu(); await h.chooseAction();
  assertHidden(h); assert.equal(h.el('call-success-view').hidden, true);
  assert.match(h.el('call-status-message').textContent, /最新一覧.*未確認/);
  await h.tick(10001); assert.equal(h.el('call-status-view').hidden, false);
  assert.equal(h.posts().length, 1); h.controller.stop();
});

for (const denial of [401, 403]) {
  test(`8445 ${denial} on background session closes menu/detail/receipts and ignores a late POST 200`, async () => {
    let expired = false; const gate = deferred();
    const h = operationHarness({ post: (_, __, ___, ____, apply) => { const result = apply(); gate.result = result; return gate.promise; },
      fetch: url => url === '/staff/session' && expired ? response({}, denial) : undefined });
    await h.start(); await h.openMenu(); const sending = h.chooseAction(); await settle();
    assert.equal(h.posts().length, 1); const saved = h.storage.getItem(OPERATION_JOURNAL_KEY);
    expired = true; await h.controller.refresh();
    assertHidden(h);
    assert.equal(h.el('call-status-message').textContent, '');
    gate.resolve(gate.result); await sending; await settle();
    assertHidden(h); assert.equal(h.el('call-success-view').hidden, true);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), saved); assert.equal(h.posts().length, 1);
    assert.ok(h.redirects.includes('/staff/login')); h.controller.stop();
  });
  for (const seatLab of [false, true]) test(`${seatLab ? 8446 : 8445} ${denial} from operation context closes UI before a new POST`, async () => {
    const h = operationHarness({ seatLab, context: (_, n) => n === 2 ? response({}, denial) : undefined });
    await h.start(); await h.openMenu(); await h.chooseAction(); assertHidden(h);
    assert.equal(h.posts().length, 0); h.controller.stop();
  });
}

test('8445 reload/relogin queries the existing ID and current context without a new POST', async () => {
  const first = operationHarness({ post: (_, __, ___, ____, apply) => { apply(); throw Error('lost'); } });
  await first.start(); await first.openMenu(); await first.chooseAction();
  const saved = JSON.parse(first.storage.getItem(OPERATION_JOURNAL_KEY)); first.controller.stop(); await settle();
  const next = operationHarness({ rows: first.rows, storage: first.storage, ledger: first.ledger }); await next.start(); await settle(); await settle();
  assert.ok(next.requests.some(r => r.url === `/staff/operations/${saved.operation_id}`));
  assert.equal(next.posts().length, 0); assert.equal(next.storage.getItem(OPERATION_JOURNAL_KEY), null);
  assert.match(next.el('call-success-message').textContent, /呼出成功/); next.controller.stop();
});

for (const manual of [false, true]) {
  test(`8445 legacy ${manual ? 'manual' : 'pending'} is recovered as call before absent is allowed`, async () => {
    const storage = new CallStorage(), id = randomUUID(), row = reception(1, { status: 'called', revision: 1,
      called_at: '2026-11-01T18:10:00+09:00', allowed_actions: ['absent'] });
    const request = { operation_id: id, action: 'call', expected_revision: 0, write_epoch: metadata().write_epoch };
    createCallJournal({ storage, now: () => 0 }).begin({ operation_id: id, reception_id: row.id, database_id: metadata().database_id, request });
    const h = operationHarness({ rows: [row], storage, now: manual ? OPERATION_RECOVERY_MS : 0,
      ledger: new Map([[id, callReceipt(row, request)]]) });
    await h.start(); await settle(); await settle();
    assert.equal(h.posts().length, 0); assert.equal(storage.getItem(CALL_JOURNAL_KEY), null);
    await h.openMenu('absent'); assert.equal(h.actionButton.textContent, '不在'); h.controller.stop();
  });
}

for (const options of [{ lockAvailable: false }, { locksSupported: false }, { storage: { getItem() { throw Error('disabled'); } } }]) {
  test(`8445 unavailable lock/storage stops actions (${JSON.stringify(options)})`, async () => {
    const h = operationHarness(options); await h.start(); await rowTrigger(rowItem(h)).emit('click');
    assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0); assert.equal(h.posts().length, 0); h.controller.stop();
  });
}

for (const bad of ['call,absent,seat', 'absent,call', 'call,absent ', 'call', null]) {
  test(`8445 exact capability rejects ${JSON.stringify(bad)} instead of falling back to call`, async () => {
    const h = operationHarness({ fetch: url => {
      if (!url.startsWith('/staff/receptions?')) return;
      const view = new URL(url, 'https://staff.localhost:8445').searchParams.get('view');
      return { ...response(listing(view === 'active' ? [reception()] : [], view)), headers: { get: k => k === 'X-Omachi-Preview' ? 'synthetic' : bad } };
    } });
    await h.start(); await rowTrigger(rowItem(h)).emit('click');
    assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null); assert.equal(h.posts().length, 0); h.controller.stop();
  });
}

test('8445 exposes neither other actions nor actions absent from allowed_actions', async () => {
  const h = operationHarness({ row: { allowed_actions: ['return', 'seat', 'cancel'] } }); await h.start();
  await rowTrigger(rowItem(h)).emit('click');
  assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0);
  assert.match(h.el('row-menu-items').textContent, /復帰.*実行不可.*案内.*実行不可.*取消.*実行不可/);
  assert.equal(h.posts().length, 0); h.controller.stop();
});

for (const [action, status] of [['call', 'called'], ['absent', 'absent']]) {
  test(`8445 saved ${action} no_change is displayed separately from a state change`, async () => {
    const storage = new CallStorage(), operation_id = randomUUID();
    const row = reception(1, { status, called_at: '2026-11-01T18:05:00+09:00', allowed_actions: status === 'called' ? ['absent'] : ['call'] });
    const request = { operation_id, action, expected_revision: 0, write_epoch: metadata().write_epoch };
    createOperationJournal({ storage, now: () => 0 }).begin({ operation_id, reception_id: row.id, database_id: metadata().database_id, request });
    const saved = { ...callReceipt(row, request, 'no_change'), action };
    const h = operationHarness({ rows: [row], storage, ledger: new Map([[operation_id, saved]]) });
    await h.start(); await settle(); await settle();
    assert.match(h.el('call-success-message').textContent, /変更なし/); assert.equal(h.posts().length, 0);
    assert.equal(rowItem(h, 0, status === 'absent' ? 'absent' : 'active').dataset.receptionId, row.id); h.controller.stop();
  });
}

test('8445 shared lock prevents a call tab and absent tab from independently starting writes', async () => {
  const held = new Set();
  const lockManager = { async request(name, options, callback) {
    assert.equal(name, 'omachi.synthetic.staff.call.v1'); assert.equal(options.ifAvailable, true);
    if (held.has(name)) return callback(null);
    held.add(name); try { return await callback({ name }); } finally { held.delete(name); }
  } };
  const first = operationHarness({ lockManager }), second = operationHarness({ lockManager,
    row: { status: 'called', called_at: '2026-11-01T18:05:00+09:00', allowed_actions: ['absent'] } });
  await first.start(); await second.start(); await first.openMenu();
  await rowTrigger(rowItem(second)).emit('click');
  assert.equal(second.el('row-menu-items').children.flatMap(x => x.children).length, 0);
  assert.equal(second.posts().length, 0); first.controller.stop(); second.controller.stop();
});

test('8445 unknown POST survives background refresh without abort, extra UUID, or a second pending action', async () => {
  const gate = deferred(); let postSignal;
  const h = operationHarness({ post: (_, request, __, ___, apply) => { gate.result = apply(); postSignal = request.signal; return gate.promise; } });
  await h.start(); await h.openMenu(); const sending = h.chooseAction(); await settle();
  const saved = h.storage.getItem(OPERATION_JOURNAL_KEY);
  for (let i = 0; i < 2; i++) await h.tick(5000);
  assert.equal(postSignal.aborted, false); assert.equal(h.posts().length, 1);
  assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), saved);
  await rowTrigger(rowItem(h)).emit('click'); assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0);
  gate.resolve(gate.result); await sending; h.controller.stop();
});

test('8445 timeout keeps the fixed request and never auto-hides or auto-reposts', async () => {
  const h = operationHarness({ post: () => new Promise(() => {}) });
  await h.start(); await h.openMenu(); const sending = h.chooseAction(); await settle();
  const saved = h.storage.getItem(OPERATION_JOURNAL_KEY);
  await h.tick(15001); await sending;
  assert.match(h.el('call-status-message').textContent, /結果不明/); assert.equal(h.posts().length, 1);
  assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), saved);
  await h.tick(5001); assert.equal(h.el('call-status-view').hidden, false); h.controller.stop();
});

for (const field of ['database_id', 'write_epoch']) {
  test(`8445 regenerated ${field} blocks old fixed requests and leaves recovery data`, async () => {
    const h = operationHarness({ meta: { [field]: randomUUID() }, post: () => { throw Error('should not POST'); } });
    const operation_id = randomUUID();
    createOperationJournal({ storage: h.storage, now: () => 0 }).begin({ operation_id, reception_id: h.rows[0].id,
      database_id: metadata().database_id, request: { operation_id, action: 'call', expected_revision: 0, write_epoch: metadata().write_epoch } });
    await h.start(); await settle(); await h.el('call-retry').emit('click');
    assert.equal(h.posts().length, 0); assert.ok(h.storage.getItem(OPERATION_JOURNAL_KEY));
    await rowTrigger(rowItem(h)).emit('click'); assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0); h.controller.stop();
  });
}

test('8445 expired manual marker is queried without replaying its discarded body', async () => {
  const storage = new CallStorage(), operation_id = randomUUID();
  createOperationJournal({ storage, now: () => 0 }).begin({ operation_id, reception_id: reception().id, database_id: metadata().database_id,
    request: { operation_id, action: 'call', expected_revision: 0, write_epoch: metadata().write_epoch } });
  const h = operationHarness({ storage, now: OPERATION_RECOVERY_MS }); await h.start(); await settle();
  assert.equal(JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY)).state, 'manual');
  assert.equal(JSON.parse(storage.getItem(OPERATION_JOURNAL_KEY)).request, undefined);
  assert.equal(h.el('call-retry').hidden, true); await h.tick(5001);
  assert.match(h.el('call-status-message').textContent, /期限超過/); assert.equal(h.posts().length, 0); h.controller.stop();
});

for (const kind of ['corrupt', 'save-failure', 'missing-guard']) {
  test(`8445 ${kind} storage failure forbids a POST`, async () => {
    const storage = new CallStorage();
    if (kind === 'corrupt') storage.setItem(OPERATION_JOURNAL_KEY, '{');
    if (kind === 'missing-guard') storage.setItem('omachi.staff.operation.guard.v2', `pending:${randomUUID()}`);
    const h = operationHarness({ storage }); await h.start();
    if (kind === 'save-failure') { await h.openMenu(); storage.fail = true; await h.chooseAction(); }
    else { await rowTrigger(rowItem(h)).emit('click'); assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0); }
    assert.equal(h.posts().length, 0); assert.match(h.el('call-status-message').textContent, /復旧情報|停止/); h.controller.stop();
  });
}

test('8445 menu Escape restores focus and success timer generations do not mutate the journal', async () => {
  const h = operationHarness(); await h.start(); await h.openMenu();
  await h.doc.emit('keydown', { key: 'Escape' });
  assert.equal(h.el('row-menu').hidden, true); assert.equal(h.doc.activeElement, rowTrigger(rowItem(h)));
  assert.equal(h.posts().length, 0);
  await h.openMenu(); await h.chooseAction();
  const firstTimer = [...h.timers.values()].find(timer => timer.at === h.env.now() + 5000)?.handler;
  await h.openMenu('absent'); await h.chooseAction();
  firstTimer?.(); assert.equal(h.el('call-success-view').hidden, false);
  h.el('call-success-close').focus(); await h.tick(6000); assert.equal(h.el('call-success-view').hidden, false);
  await h.el('call-success-close').emit('click'); assert.equal(h.el('call-success-view').hidden, true);
  assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null); assert.equal(h.rows[0].status, 'absent'); h.controller.stop();
});

for (const closing of ['logout', 'expiry']) {
  test(`8445 ${closing} closes UI, keeps a pending request and discards its late response`, async () => {
    const gate = deferred();
    const h = operationHarness({ post: (_, __, ___, ____, apply) => { gate.result = apply(); return gate.promise; } });
    await h.start(); await h.openMenu(); const sending = h.chooseAction(); await settle();
    const saved = h.storage.getItem(OPERATION_JOURNAL_KEY);
    if (closing === 'logout') await h.controller.logout();
    else { h.doc.visibilityState = 'hidden'; await h.doc.emit('visibilitychange'); await h.tick(900001); }
    assertHidden(h); gate.resolve(gate.result); await sending; await settle(); await settle();
    assert.equal(h.el('call-success-view').hidden, true);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), saved); assert.equal(h.posts().length, 1); h.controller.stop();
  });
}

test('8445 background capability loss closes the action menu and cannot enable a different journal', async () => {
  let lost = false;
  const h = operationHarness({ fetch: url => {
    if (!lost || url !== '/staff/session') return;
    return { ...response(session()), headers: { get: k => k === 'X-Omachi-Preview' ? 'synthetic' : 'call' } };
  } });
  await h.start(); await h.openMenu(); lost = true; await h.controller.refresh();
  await h.chooseAction();
  assert.equal(h.posts().length, 0); assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null); h.controller.stop();
});

test('8445 capability on another port never initializes the common journal or operation menu', async () => {
  const h = operationHarness(); h.env.location.port = '8443'; await h.start(); await rowTrigger(rowItem(h)).emit('click');
  assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0);
  assert.equal(h.storage.getItem('omachi.staff.operation.guard.v2'), null); assert.equal(h.posts().length, 0); h.controller.stop();
});

test('8445 a newer background row invalidates an older final-context response before UUID or POST', async () => {
  const gate = deferred(); let prepared;
  const h = operationHarness({ context: (row, n) => {
    if (n === 1) prepared = { ...row };
    if (n === 2) return gate.promise;
  } });
  await h.start(); await h.openMenu(); const confirming = h.chooseAction(); await settle();
  h.rows[0] = { ...h.rows[0], status: 'called', revision: 1, called_at: '2026-11-01T18:10:00+09:00', allowed_actions: ['absent'] };
  await h.controller.refresh();
  gate.resolve(response({ metadata: metadata(), reception: prepared })); await confirming; await settle(); await settle();
  assert.equal(h.posts().length, 0); assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null);
  assert.equal(h.rows[0].status, 'called'); assert.match(h.el('call-status-message').textContent, /確認|選び直/); h.controller.stop();
});

// Three UI sheets remain projections of the same verified two-source snapshot.
test('three sheets classify every row once, preserve each source order and distinguish tab counts from the unseated total', async () => {
  const rows = [reception(1, { status: 'absent' }), reception(2, { status: 'called' }),
    reception(3), reception(4, { status: 'absent' }), reception(5)];
  const recent = [seatedRow(9), seatedRow(6)];
  const before = JSON.stringify({ rows, recent });
  const h = harness({ rows, recent }); await h.start();
  const ids = view => h.el(`${view}-list`).children.map(item => item.dataset.receptionId);
  assert.deepEqual(ids('active'), [rows[1].id, rows[2].id, rows[4].id]);
  assert.deepEqual(ids('absent'), [rows[0].id, rows[3].id]);
  assert.deepEqual(ids('recent'), recent.map(row => row.id));
  const all = [...ids('active'), ...ids('absent'), ...ids('recent')];
  assert.equal(new Set(all).size, 7);
  assert.deepEqual(['active', 'absent', 'recent'].map(prefix => h.el(`${prefix}-tab-count`).textContent), ['3', '2', '2']);
  assert.equal(h.el('unseated-count').textContent, '5');
  assert.deepEqual(['waiting-count', 'called-count', 'absent-tab-count'].map(id => h.el(id).textContent), ['2', '1', '2']);
  assert.equal(JSON.stringify({ rows, recent }), before, 'display classification cannot mutate saved source data');
  assert.deepEqual(h.requests.filter(r => r.url.startsWith('/staff/receptions?')).map(r => {
    const params = new URL(r.url, h.env.location.origin).searchParams;
    assert.equal(params.get('hours'), '24'); return params.get('view');
  }), ['active', 'recently_seated']);
  assertReadOnlyRequests(h.requests); h.controller.stop();
});

test('absent-only data is visible in its own sheet and remains part of the unseated total', async () => {
  const h = harness({ rows: [reception(1, { status: 'absent' })] }); await h.start();
  assert.equal(h.el('active-list').children.length, 0);
  assert.equal(h.el('active-empty').hidden, false);
  assert.equal(h.el('active-tab-count').textContent, '0');
  assert.equal(h.el('absent-tab-count').textContent, '1');
  assert.equal(h.el('unseated-count').textContent, '1');
  await h.el('absent-tab').emit('click');
  assert.equal(h.el('absent-panel').hidden, false);
  assert.equal(rowItem(h, 0, 'absent').dataset.receptionId, reception().id);
  assertReadOnlyRequests(h.requests); h.controller.stop();
});

test('three-sheet counts and rows commit only after the last page of both API sources is verified', async () => {
  const gate = deferred(); let delayed = false;
  const rows = Array.from({ length: 151 }, (_, index) => reception(index + 1, { status: index % 2 ? 'absent' : 'waiting' }));
  const h = harness({ fetch: async url => {
    if (url === '/staff/session') return response(session());
    const params = new URL(url, 'https://staff.localhost').searchParams, view = params.get('view');
    const meta = metadata({ view_version: (delayed ? 'b' : 'a').repeat(64) });
    if (view === 'recently_seated') return delayed ? gate.promise : response(listing([], view, { metadata: meta }));
    const source = delayed ? rows : rows.slice(0, 1);
    return response(listing(params.has('cursor') ? source.slice(100) : source.slice(0, 100), view,
      { metadata: meta, has_more: source.length > 100 && !params.has('cursor'),
        next_cursor: source.length > 100 && !params.has('cursor') ? 'page-two' : null }));
  } });
  await h.start(); const first = rowItem(h);
  delayed = true; const fetching = h.controller.refresh(); await settle();
  assert.equal(rowItem(h), first); assert.equal(h.el('absent-list').children.length, 0);
  assert.equal(h.el('unseated-count').textContent, '1');
  assert.equal(h.el('absent-tab-count').textContent, '0');
  gate.resolve(response(listing([seatedRow(200)], 'recently_seated', { metadata: metadata({ view_version: 'b'.repeat(64) }) })));
  await fetching;
  assert.deepEqual(['active', 'absent', 'recent'].map(view => h.el(`${view}-list`).children.length), [76, 75, 1]);
  assert.deepEqual(['active', 'absent', 'recent'].map(view => h.el(`${view}-tab-count`).textContent), ['76', '75', '1']);
  assert.equal(h.el('unseated-count').textContent, '151');
  assert.equal(rowItem(h), first); assertReadOnlyRequests(h.requests); h.controller.stop();
});

test('all three sheets keep independent scroll positions and absent details use the original reception ID', async () => {
  const h = harness({ rows: [reception(1), reception(2, { status: 'absent' })], recent: [seatedRow(3)] }); await h.start();
  const requestCount = h.requests.length;
  for (const [prefix, top] of [['active', 120], ['absent', 260], ['recent', 380]]) {
    await h.el(`${prefix}-tab`).emit('click'); assert.equal(h.el('reception-scroll').scrollTop, 0);
    h.el('reception-scroll').scrollTop = top;
  }
  for (const [prefix, top] of [['active', 120], ['absent', 260], ['recent', 380]]) {
    await h.el(`${prefix}-tab`).emit('click'); assert.equal(h.el('reception-scroll').scrollTop, top);
  }
  assert.equal(h.requests.length, requestCount);
  await h.el('absent-tab').emit('click');
  await rowItem(h, 0, 'absent').children[0].emit('click');
  assert.match(h.el('detail-title').textContent, /002/);
  assert.ok(h.requests.at(-1).url.includes(reception(2).id));
  assertReadOnlyRequests(h.requests); h.controller.stop();
});

test('twenty-five absent-sheet polls preserve row nodes, menu, focus, scroll, counts and single tab listeners', async () => {
  const h = harness({ rows: [reception(1), reception(2, { status: 'absent' }), reception(3, { status: 'absent' })] });
  await h.start(); await h.el('absent-tab').emit('click');
  const first = rowItem(h, 0, 'absent'), second = rowItem(h, 1, 'absent');
  await rowTrigger(second).emit('click'); h.el('reception-scroll').scrollTop = 275;
  const focused = h.doc.activeElement;
  for (let i = 0; i < 25; i++) {
    await h.tick(5001);
    assert.equal(rowItem(h, 0, 'absent'), first); assert.equal(rowItem(h, 1, 'absent'), second);
    assert.equal(h.doc.activeElement, focused); assert.equal(h.el('row-menu').hidden, false);
    assert.equal(h.el('reception-scroll').scrollTop, 275);
    assert.equal(h.el('absent-tab').getAttribute('aria-selected'), 'true');
    assert.equal(h.el('absent-tab-count').textContent, '2'); assert.equal(h.el('unseated-count').textContent, '3');
  }
  for (const prefix of ['active', 'absent', 'recent']) assert.equal(h.el(`${prefix}-tab`).listeners.get('click').length, 1);
  h.controller.stop();
  for (const prefix of ['active', 'absent', 'recent']) assert.equal(h.el(`${prefix}-tab`).listeners.get('click').length, 0);
});

test('a row moving sheets closes its stale menu, preserves the chosen sheet and gives focus a visible destination', async () => {
  const rows = [reception(), reception(2)]; const h = harness({ rows }); await h.start();
  const unchanged = rowItem(h, 1);
  const trigger = rowTrigger(rowItem(h)); trigger.focus();
  rows[0] = reception(1, { status: 'absent', revision: 1 });
  await h.controller.refresh();
  assert.equal(h.el('active-tab').getAttribute('aria-selected'), 'true');
  assert.equal(h.doc.activeElement, h.el('active-tab'));
  assert.equal(rowItem(h), unchanged); assert.equal(rowItem(h, 0, 'absent').dataset.receptionId, rows[0].id);
  await h.el('absent-tab').emit('click'); await rowTrigger(rowItem(h, 0, 'absent')).emit('click');
  rows[0] = reception(1, { status: 'called', revision: 2 });
  await h.controller.refresh();
  assert.equal(h.el('row-menu').hidden, true); assert.equal(h.el('absent-tab').getAttribute('aria-selected'), 'true');
  assert.deepEqual(h.el('active-list').children.map(row => row.dataset.receptionId), rows.map(row => row.id));
  assert.equal(h.el('absent-list').children.length, 0);
  assertReadOnlyRequests(h.requests); h.controller.stop();
});

test('a changed row staying in the same sheet restores its focused control without changing another row', async () => {
  const rows = [reception(), reception(2)]; const h = harness({ rows }); await h.start();
  const other = rowItem(h, 1); rowTrigger(rowItem(h)).focus();
  rows[0] = reception(1, { status: 'called', revision: 1 });
  await h.controller.refresh();
  assert.equal(h.doc.activeElement, rowTrigger(rowItem(h))); assert.equal(rowItem(h, 1), other);
  assert.equal(h.el('active-tab').getAttribute('aria-selected'), 'true'); h.controller.stop();
});

for (const [port, factory] of [[8444, callHarness], [8445, operationHarness]]) {
  test(`${port} opening a menu permits keyboard tab changes without a POST`, async () => {
    const h = factory(); await h.start();
    await (port === 8444 ? h.openCallMenu() : h.openMenu());
    await h.el('active-tab').emit('keydown', { key: 'End' });
    assert.equal(h.el('recent-tab').getAttribute('aria-selected'), 'true');
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 0); h.controller.stop();
  });
  test(`${port} a selected operation survives tab navigation while initial context is delayed`, async () => {
    const gate = deferred();
    const h = factory({ context: (_row, count) => count === 1 ? gate.promise : undefined }); await h.start();
    await (port === 8444 ? h.openCallMenu() : h.openMenu());
    const sending = port === 8444 ? h.chooseCall() : h.chooseAction(); await settle();
    await h.el('absent-tab').emit('click');
    assert.equal(h.el('absent-tab').getAttribute('aria-selected'), 'true');
    gate.resolve(response({ metadata: metadata(), reception: reception() })); await sending; await settle(); await settle();

    assert.equal(h.el('absent-tab').getAttribute('aria-selected'), 'true');
    assert.equal(h.requests.filter(r => r.method === 'POST').length, 1); h.controller.stop();
  });
}

for (const action of ['call', 'absent']) {
  test(`8445 ${action} in flight and result-unknown recovery survive all sheet changes with one fixed journal`, async () => {
    const gate = deferred(); let missing = true;
    const h = operationHarness({ row: action === 'absent' ? { status: 'called', called_at: '2026-11-01T18:05:00+09:00', allowed_actions: ['absent'] } : {},
      post: (_body, request, _rows, _ledger, apply) => { gate.result = apply(); gate.signal = request.signal; return gate.promise; },
      lookup: () => missing ? response({}, 404) : undefined });
    await h.start(); await h.openMenu(action); const sending = h.chooseAction(); await settle();
    const saved = h.storage.getItem(OPERATION_JOURNAL_KEY);
    assert.ok(saved);
    for (const prefix of ['absent', 'recent', 'active']) {
      await h.el(`${prefix}-tab`).emit('click');
      assert.equal(h.el(`${prefix}-panel`).hidden, false);
      assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), saved);
    }
    await h.tick(5001); assert.equal(gate.signal.aborted, false);
    gate.reject(Error('synthetic lost response')); await sending;
    await h.el('call-lookup').emit('click'); await settle();
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), saved);
    assert.equal(h.posts().length, 1);
    assert.match(h.el('call-status-message').textContent, /結果不明/);
    missing = false; await h.el('recent-tab').emit('click');
    await h.el('call-lookup').emit('click'); await settle(); await settle();
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null);
    assert.equal(h.posts().length, 1);
    assert.equal(h.el('recent-tab').getAttribute('aria-selected'), 'true');
    assert.match(h.el('call-success-message').textContent, /最新一覧/); h.controller.stop();
  });
}

test('a server-confirmed seating correction moves between sheets without reconstructing or rewriting history', async () => {
  const rows = [reception(2)], recent = [seatedRow(1)]; const h = harness({ rows, recent }); await h.start();
  await h.el('recent-tab').emit('click');
  // Only emulate responses from the existing API; no correction write path.
  recent.splice(0, 1); rows.unshift(reception(1, { revision: 2 }));
  await h.controller.refresh();
  assert.equal(h.el('recent-list').children.length, 0);
  assert.deepEqual(h.el('active-list').children.map(row => row.dataset.receptionId), rows.map(row => row.id));
  assert.equal(h.el('recent-tab').getAttribute('aria-selected'), 'true');
  rows.shift(); recent.push(seatedRow(1)); recent[0] = { ...recent[0], revision: 3,
    valid_seating_event: { ...recent[0].valid_seating_event, event_id: 2000 } };
  await h.controller.refresh();
  assert.equal(h.el('recent-tab-count').textContent, '1');
  assert.equal(rowItem(h, 0, 'recent').dataset.receptionId, reception().id);
  assertReadOnlyRequests(h.requests); h.controller.stop();
});

for (const failure of [401, 403, 409, 'network']) {
  test(`all three sheets and counts fail closed after ${failure}`, async () => {
    let failing = false;
    const h = harness({ rows: [reception(), reception(2, { status: 'absent' })], recent: [seatedRow(3)],
      fetch: async url => {
        if (failing && url === '/staff/session') {
          if (failure === 'network') throw Error('synthetic disconnection');
          return response({}, failure);
        }
      } });
    await h.start(); await h.el('absent-tab').emit('click'); failing = true;
    await h.controller.refresh(); assertHidden(h);
    for (const prefix of ['active', 'absent', 'recent']) {
      assert.equal(h.el(`${prefix}-tab-count`).textContent, '—'); assert.equal(h.el(`${prefix}-tab`).disabled, true);
    }
    assert.equal(h.el('unseated-count').textContent, '—'); h.controller.stop();
  });
}

for (const status of ['waiting', 'called']) {
  test(`8446 directly seats the selected ${status} reception once`, async () => {
    const target = reception(2, { status, called_at: status === 'called' ? '2026-11-01T18:05:00+09:00' : null,
      allowed_actions: ['call', 'absent', 'seat'] });
    const gate = deferred();
    const h = operationHarness({ seatLab: true, rows: [reception(), target], post: async (_, __, ___, ____, apply) => {
      await gate.promise; return apply();
    } });
    await h.start();
    await rowTrigger(rowItem(h, 1)).emit('click');
    assert.deepEqual(h.el('row-menu-items').children.flatMap(x => x.children.map(b => b.textContent)), ['案内完了']);
    await h.openMenu('seat', 1);
    assert.match(h.el('row-menu-title').textContent, /002番/);
    assert.ok(h.el('row-menu-status').textContent.includes(status === 'waiting' ? '待機中' : '呼出中'));
    assert.equal(h.posts().length, 0);
    await Promise.all([h.chooseAction(), h.chooseAction()]);
    assert.equal(h.posts().length, 1);
    assert.equal(h.posts()[0].url, `/staff/receptions/${target.id}/operations`);
    assert.equal(h.el('unseated-count').textContent, '2');
    assert.equal(h.el('recent-list').children.length, 0);
    gate.resolve(); await settle(); await settle(); await settle();
    assert.equal(h.el('active-tab').getAttribute('aria-selected'), 'true');
    assert.equal(rowItem(h).dataset.receptionId, reception().id);
    assert.equal(rowItem(h, 0, 'recent').dataset.receptionId, target.id);
    assert.deepEqual(['active-tab-count', 'recent-tab-count', 'unseated-count'].map(k => h.el(k).textContent), ['1', '1', '1']);
    assert.match(rowItem(h, 0, 'recent').textContent, /18:25/);
    assert.match(h.el('call-success-message').textContent, /案内完了成功.*最新一覧/);
    assert.deepEqual(h.posts().map(r => JSON.parse(r.body).action), ['seat']);
    h.controller.stop();
  });
}

test('8446 does not offer seat on absent or seated rows even with an extra allowed action', async () => {
  const h = operationHarness({ seatLab: true, rows: [reception(1, { status: 'absent', allowed_actions: ['seat', 'call'] }),
    { ...seatedRow(2), allowed_actions: ['seat', 'absent'] }, reception(3, { status: 'cancelled' })] });
  await h.start();
  for (const view of ['absent', 'recent']) {
    await h.el(`${view}-tab`).emit('click'); await rowTrigger(rowItem(h, 0, view)).emit('click');
    assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0);
  }
  assert.doesNotMatch(h.el('row-menu-items').textContent, /案内完了/);
  assert.equal(h.el('active-list').children.length, 0);
  assert.equal(h.posts().length, 0); h.controller.stop();
});

test('8446 recovers the lost seat result by ID after login, with no second POST', async () => {
  const first = operationHarness({ seatLab: true, post: (_, __, ___, ____, apply) => { apply(); throw Error('lost'); } });
  await first.start(); await first.openMenu(); await first.chooseAction();
  assert.match(first.el('call-status-message').textContent, /結果不明/);
  const saved = JSON.parse(first.storage.getItem(OPERATION_JOURNAL_KEY));
  await rowTrigger(rowItem(first)).emit('click');
  assert.equal(first.el('row-menu-items').children.flatMap(x => x.children).length, 0);
  first.controller.stop(); await settle();
  const next = operationHarness({ seatLab: true, rows: first.rows, storage: first.storage, ledger: first.ledger });
  await next.start(); await settle(); await settle();
  assert.ok(next.requests.some(r => r.url === `/staff/operations/${saved.operation_id}`));
  assert.equal(next.posts().length, 0); assert.equal(next.storage.getItem(OPERATION_JOURNAL_KEY), null);
  assert.match(next.el('call-success-message').textContent, /案内完了成功/);
  assert.equal(next.el('unseated-count').textContent, '0'); next.controller.stop();
});

test('8446 mismatched session capability closes the menu before a delayed list completes', async () => {
  let lost = false; const gate = deferred();
  const h = operationHarness({ seatLab: true, fetch: async url => {
    if (lost && url === '/staff/session') return { ...response(session()), headers: { get: k => k === 'X-Omachi-Preview' ? 'synthetic' : 'call' } };
    if (lost && url.startsWith('/staff/receptions?')) await gate.promise;
  } });
  await h.start(); await h.openMenu(); lost = true; const poll = h.controller.refresh(); await settle();

  await h.chooseAction(); assert.equal(h.posts().length, 0);
  gate.resolve(); await poll; await rowTrigger(rowItem(h)).emit('click');
  assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0);
  h.controller.stop();
});

for (const port of ['8443', '8444', '8445']) {
  test(`seat headers on ${port} cannot enable the seat UI`, async () => {
    const h = operationHarness({ seatLab: true });
    h.env.location.port = port;
    await h.start(); await rowTrigger(rowItem(h)).emit('click');
    assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0);
    assert.equal(h.posts().length, 0); h.controller.stop();
  });
}

test('8446 requires seat on every page of both lists', async () => {
  const h = operationHarness({ seatLab: true, fetch: url => {
    if (!url.startsWith('/staff/receptions?')) return;
    const params = new URL(url, 'https://staff.localhost:8446').searchParams;
    if (params.get('view') !== 'active') return;
    if (!params.has('cursor')) return response(listing([reception()], 'active', { has_more: true, next_cursor: 'p2' }));
    return { ...response(listing([reception(2)])), headers: { get: k => k === 'X-Omachi-Preview' ? 'synthetic' : 'call,absent' } };
  } });
  await h.start(); await rowTrigger(rowItem(h)).emit('click');
  assert.equal(h.el('row-menu-items').children.flatMap(x => x.children).length, 0);
  assert.equal(h.posts().length, 0); h.controller.stop();
});

test('restoring the seat capability cannot revive an in-flight invalidated selection', async () => {
  let lost = false; const gate = deferred();
  const h = operationHarness({ seatLab: true, context: (_, n) => n === 2 ? gate.promise : undefined,
    fetch: url => lost && url === '/staff/session'
      ? { ...response(session()), headers: { get: () => null } } : undefined });
  await h.start(); await h.openMenu(); const confirming = h.chooseAction(); await settle();
  lost = true; await h.controller.refresh();
  lost = false; await h.controller.refresh();
  gate.resolve(response({ metadata: metadata(), reception: reception() })); await confirming; await settle();
  assert.equal(h.posts().length, 0); assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null);
  h.controller.stop();
});

for (const [port, action, factory] of [
  [8444, 'call', callHarness], [8445, 'call', operationHarness],
  [8445, 'absent', operationHarness], [8446, 'seat', options => operationHarness({ ...options, seatLab: true })],
]) {
  test(`${port} ${action}: swipe/menu send nothing; one explicit selection validates twice and consecutive rows work`, async () => {
    const initial = action === 'absent' ? { status: 'called', called_at: '2026-11-01T18:05:00+09:00', allowed_actions: ['absent'] } : {};
    const h = factory({ rows: [reception(1, initial), reception(2, initial)] });
    await h.start();
    const posts = () => h.requests.filter(r => r.method === 'POST' && r.url.endsWith('/operations'));
    const label = { call: '呼出', absent: '不在', seat: '案内完了' }[action];
    h.el('reception-scroll').scrollTop = 135;
    for (let i = 0; i < 2; i++) {
      const item = rowItem(h, action === 'call' ? i : 0), id = item.dataset.receptionId;
      await swipe(h, item);
      assert.equal(posts().length, i);
      assert.equal(h.el('row-menu').hidden, false);
      const buttons = h.el('row-menu-items').children.flatMap(x => x.children);
      const button = buttons.find(b => b.textContent === label);
      assert.ok(button);
      assert.equal(button.getAttribute('aria-describedby'), 'row-menu-title row-menu-status row-menu-note');
      assert.match(h.el('row-menu-status').textContent, /大人 2名.*子供 1名/);
      button.focus();
      await Promise.all([button.emit('click'), button.emit('click'), button.emit('click')]);
      await settle(); await settle();
      assert.equal(posts().length, i + 1);
      assert.equal(posts()[i].url, `/staff/receptions/${id}/operations`);
      assert.equal(JSON.parse(posts()[i].body).action, action);
      assert.ok(h.requests.filter(r => r.url === `/staff/receptions/${id}/operation-context`).length >= 3);
      assert.equal(h.el('active-tab').getAttribute('aria-selected'), 'true');
      assert.equal(h.el('reception-scroll').scrollTop, 135);
      assert.match(h.el('call-success-message').textContent, /成功.*最新一覧/);
      if (action === 'seat') assert.equal(h.doc.activeElement, h.el('active-tab'));
      else if (action === 'absent') {
        assert.equal(h.doc.activeElement, h.el('active-tab'));
        await h.el('active-tab').emit('click');
      } else assert.equal(h.doc.activeElement, rowTrigger(rowItem(h, i)));
    }
    assert.equal(new Set(posts().map(p => JSON.parse(p.body).operation_id)).size, 2);
    h.controller.stop();
  });

  for (const change of ['removed', 'revision', 'state', 'auth']) {
    test(`${port} ${action}: a ${change} change during the second context prevents UUID and POST`, async () => {
      const gate = deferred(); let selected;
      const h = factory({ row: action === 'absent' ? { status: 'called', called_at: '2026-11-01T18:05:00+09:00', allowed_actions: ['absent'] } : {},
        context: (row, n) => {
          if (n === 1) selected = { ...row };
          return n === 2 ? gate.promise : undefined;
        } });
      let issued = 0; h.env.crypto.randomUUID = () => { issued++; return randomUUID(); };
      await h.start();
      if (port === 8444) await h.openCallMenu(); else await h.openMenu(action);
      const request = port === 8444 ? h.chooseCall() : h.chooseAction(); await settle();
      if (change === 'auth') await h.controller.logout();
      else {
        if (change === 'removed') h.rows.splice(0, 1);
        else h.rows[0] = { ...h.rows[0], revision: h.rows[0].revision + 1,
          ...(change === 'state' ? { status: 'absent', called_at: '2026-11-01T18:05:00+09:00' } : {}) };
        await h.controller.refresh();
      }
      gate.resolve(response({ metadata: metadata(), reception: selected })); await request; await settle();
      assert.equal(issued, 0);
      assert.equal(h.requests.filter(r => r.method === 'POST' && r.url.endsWith('/operations')).length, 0);
      h.controller.stop();
    });
  }
}

// #14: while reception A's operation waits on the network (context or POST) or
// is result-unknown, a different reception B can neither start, contact the
// server, issue an operation ID nor overwrite A's recovery data. Progress is
// controlled by deferred promises and the fake clock only.
for (const [port, stage] of [[8445, 'context'], [8445, 'post'], [8445, 'unknown'],
  [8446, 'context'], [8446, 'post'], [8446, 'unknown']]) {
  const waiting = stage === 'unknown' ? 'result-unknown' : `waiting on its ${stage === 'post' ? 'POST' : 'operation-context'}`;
  test(`${port} reception B cannot start while reception A is ${waiting}, and B works after A settles`, async () => {
    const seatLab = port === 8446, word = { call: '呼出', absent: '不在', seat: '案内完了' };
    const actionA = seatLab ? 'seat' : 'call', actionB = seatLab ? 'seat' : 'absent';
    const rowA = reception(1), rowB = seatLab ? reception(2) : reception(2, { status: 'called',
      called_at: '2026-11-01T18:05:00+09:00', allowed_actions: ['absent', 'seat', 'cancel'] });
    const gate = deferred(); let held = false, posted = 0;
    const h = operationHarness({ seatLab, rows: [rowA, rowB],
      // Hold only A's first context, so a stray B request could never hang the test.
      context: row => {
        if (stage !== 'context' || held || row?.id !== rowA.id) return undefined;
        held = true; return gate.promise;
      },
      post: async (_body, _request, _rows, _ledger, apply) => {
        if (posted++) return undefined;
        if (stage === 'post') { await gate.promise; return apply(); }
        if (stage === 'unknown') { apply(); throw Error('synthetic lost response'); }
        return undefined;
      } });
    let issued = 0; h.env.crypto.randomUUID = () => { issued++; return randomUUID(); };
    const locate = id => {
      for (const view of ['active', 'absent', 'recent']) {
        const item = h.el(`${view}-list`).children.find(child => child.dataset.receptionId === id);
        if (item) return { view, item };
      }
      return assert.fail(`reception ${id} is not listed`);
    };
    const openRow = async id => {
      const { view, item } = locate(id);
      await h.el(`${view}-tab`).emit('click'); await rowTrigger(item).emit('click');
      return item;
    };
    const buttons = () => h.el('row-menu-items').children.flatMap(item => item.children);
    await h.start();

    // B is operable before A starts; keep its button to press it later directly.
    const itemB = await openRow(rowB.id);
    assert.match(h.el('row-menu-title').textContent, /002番/);
    const staleB = buttons().find(button => button.textContent === word[actionB]);
    assert.ok(staleB, 'B offers its action while nothing is in flight');

    await h.openMenu(actionA, 0);
    assert.match(h.el('row-menu-title').textContent, /001番/);
    const sending = h.chooseAction(); await settle();
    if (stage === 'context') {
      assert.equal(held, true); assert.equal(h.posts().length, 0);
      assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null); assert.equal(issued, 0);
    } else {
      assert.equal(h.posts().length, 1);
      assert.equal(h.posts()[0].url, `/staff/receptions/${rowA.id}/operations`);
      assert.ok(h.storage.getItem(OPERATION_JOURNAL_KEY)); assert.equal(issued, 1);
    }
    if (stage === 'unknown') assert.match(h.el('call-status-message').textContent, /結果不明/);
    else assert.ok(h.el('call-status-message').textContent.includes(`${rowA.display_number}の${word[actionA]}を処理中です`));
    // A's operation ID comes from its recovery journal; the context stage has none yet.
    const idA = stage === 'context' ? null : JSON.parse(h.storage.getItem(OPERATION_JOURNAL_KEY)).operation_id;
    if (idA) assert.equal(JSON.parse(h.posts()[0].body).operation_id, idA);
    const shownId = idA ? `操作ID：${idA}` : '';

    const journal = h.storage.getItem(OPERATION_JOURNAL_KEY), guard = h.storage.getItem(OPERATION_JOURNAL_GUARD_KEY);
    const stored = [...h.storage.values], message = h.el('call-status-message').textContent;
    const posts = h.posts().length, uuids = issued, originalB = structuredClone(h.rows[1]), since = h.requests.length;
    const attemptMenu = async () => {
      const item = await openRow(rowB.id);
      assert.match(h.el('row-menu-title').textContent, /002番/);
      assert.equal(buttons().length, 0, 'B offers no executable action while A is unresolved');
      return item;
    };

    assert.equal(await attemptMenu(), itemB);
    // While A's POST is in flight the ID appears only at the next render, so
    // before B is tried the display may still be blank, but never another ID.
    assert.ok(['', shownId].includes(h.el('call-operation-id').textContent), 'only A\'s operation ID can be displayed');
    await Promise.all([staleB.emit('click'), staleB.emit('click')]); await settle();
    assert.equal(h.doc.activeElement, rowTrigger(itemB), 'the kept B button passed its row checks and reached execute');
    assert.equal(h.el('call-operation-id').textContent, shownId);
    await h.tick(5000);
    assert.ok(h.requests.slice(since).some(r => r.url.startsWith('/staff/receptions?')), 'a background refresh ran');
    assert.equal(await attemptMenu(), itemB, 'the unchanged B row was not redrawn');

    assert.deepEqual(h.requests.slice(since).filter(r => r.url.includes('/operation')).map(r => r.url), []);
    assert.deepEqual(h.requests.filter(r => r.url.includes(rowB.id)).map(r => r.url), []);
    assert.equal(h.posts().length, posts); assert.equal(issued, uuids);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), journal);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_GUARD_KEY), guard);
    assert.deepEqual([...h.storage.values], stored);
    assert.equal(h.el('call-operation-id').textContent, shownId);
    assert.equal(h.el('call-status-message').textContent, message);
    assert.deepEqual(h.rows[1], originalB);

    if (stage === 'context') gate.resolve(response({ metadata: metadata(), reception: { ...rowA } }));
    if (stage === 'post') gate.resolve();
    await sending;
    if (stage === 'unknown') await h.el('call-lookup').emit('click');
    await settle(); await settle(); await settle();
    assert.equal(h.posts().length, 1);
    assert.equal(h.posts()[0].url, `/staff/receptions/${rowA.id}/operations`);
    assert.equal(JSON.parse(h.posts()[0].body).action, actionA);
    if (idA) assert.equal(JSON.parse(h.posts()[0].body).operation_id, idA);
    assert.equal(issued, 1);
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null);
    assert.equal(h.el('call-operation-id').textContent, '');
    assert.equal(h.rows[0].status, seatLab ? 'seated' : 'called');
    assert.equal(locate(rowA.id).view, seatLab ? 'recent' : 'active');
    assert.match(locate(rowA.id).item.textContent, seatLab ? /案内済み/ : /呼出中/);
    assert.deepEqual(h.rows[1], originalB); assert.equal(locate(rowB.id).view, 'active');
    assert.match(h.el('call-success-message').textContent, new RegExp(`${word[actionA]}成功.*最新一覧`));

    await openRow(rowB.id);
    const buttonB = buttons().find(button => button.textContent === word[actionB]);
    assert.ok(buttonB, 'B is operable again once A has settled');
    await buttonB.emit('click'); await settle(); await settle();
    assert.equal(h.posts().length, 2);
    assert.equal(h.posts()[1].url, `/staff/receptions/${rowB.id}/operations`);
    const [first, second] = h.posts().map(p => JSON.parse(p.body));
    assert.equal(second.action, actionB); assert.notEqual(second.operation_id, first.operation_id);
    assert.equal(issued, 2);
    assert.equal(h.rows[1].status, seatLab ? 'seated' : 'absent');
    assert.equal(h.storage.getItem(OPERATION_JOURNAL_KEY), null);
    assert.match(h.el('call-success-message').textContent, new RegExp(`${word[actionB]}成功.*最新一覧`));
    h.controller.stop();
  });
}

test('8444 a restored capability does not revive the earlier selected call', async () => {
  let lost = false; const gate = deferred();
  const h = callHarness({ context: (_, n) => n === 2 ? gate.promise : undefined,
    fetch: url => lost && url === '/staff/session'
      ? { ...response(session()), headers: { get: () => null } } : undefined });
  await h.start(); await h.openCallMenu(); const pending = h.chooseCall(); await settle();
  lost = true; await h.controller.refresh();
  lost = false; await h.controller.refresh();
  gate.resolve(response({ metadata: metadata(), reception: reception() })); await pending; await settle();
  assert.equal(h.postRequests().length, 0);
  assert.equal(h.storage.getItem(CALL_JOURNAL_KEY), null);
  assert.match(h.el('call-status-message').textContent, /変わりました|選び直/);
  h.controller.stop();
});
