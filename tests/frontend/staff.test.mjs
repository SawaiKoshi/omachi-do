import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { createStaffApp } from '../../web/staff/assets/staff.mjs';

// The browser suite deliberately provides no real browser storage, cookies, DB,
// credentials, or network. All receipts and session responses are synthetic.
const IDS = [
  'login-view', 'login-form', 'staff-token', 'login-button', 'login-message',
  'staff-view', 'logout-button', 'refresh-button', 'status-message', 'updated-at',
  'waiting-count', 'called-count', 'absent-count', 'active-list', 'recent-list',
  'detail-view', 'detail-title', 'detail-content', 'detail-close',
  'active-empty', 'recent-empty', 'security-note',
  'active-tab', 'recent-tab', 'active-panel', 'recent-panel', 'reception-scroll',
  'row-menu', 'row-menu-title', 'row-menu-status', 'row-menu-items', 'row-menu-close', 'synthetic-preview',
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
    this.hidden = ['staff-view', 'detail-view', 'row-menu', 'recent-panel', 'synthetic-preview'].includes(id);
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

function harness({ fetch: fetchImpl, authenticated = true, rows = [], recent = [], now = 0, page = 'receptions' } = {}) {
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
    location: { protocol: 'https:', hostname: 'staff.localhost', origin: 'https://staff.localhost', href: 'https://staff.localhost/staff/', replace(path) { redirects.push(path); } },
    now() { return time; },
    setTimeout(handler, delay) { timerId += 1; timers.set(timerId, { handler, at: time + delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  });
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(env, name, { get() { throw new Error(`Staff UI must not use ${name}`); } });
  }
  const fetcher = async (url, options = {}) => {
    requests.push({ url: String(url), ...options });
    if (fetchImpl) return fetchImpl(String(url), options);
    if (url === '/staff/session') {
      if ((options.method ?? 'GET') === 'DELETE') return response({ authenticated: false });
      return authenticated ? response(session()) : response({ detail: 'Unauthorized' }, 401);
    }
    const parsed = new URL(String(url), env.location.origin);
    if (parsed.pathname === '/staff/receptions') {
      return response(listing(parsed.searchParams.get('view') === 'recently_seated' ? recent : rows,
        parsed.searchParams.get('view') ?? 'active'));
    }
    const found = [...rows, ...recent].find(row => parsed.pathname === `/staff/receptions/${row.id}/operation-context`);
    if (found) return response({ metadata: metadata(), reception: found });
    assert.fail(`Unexpected staff request: ${options.method ?? 'GET'} ${url}`);
  };
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
  assert.equal(h.el('absent-count').textContent, '1');
  const text = h.el('active-list').textContent;
  assert.ok(text.indexOf('001') < text.indexOf('002'));
  assert.ok(text.indexOf('002') < text.indexOf('003'));
  assert.match(text, /2026/);
  assert.match(text, /大人/);
  assert.match(text, /子供|子ども/);
  assert.match(text, /待機|待ち/);
  assert.match(text, /呼出|呼び出/);
  assert.match(text, /不在/);
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

const rowItem = (h, index = 0, view = 'active') => h.el(view === 'active' ? 'active-list' : 'recent-list').children[index];
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
  assert.equal(h.el('active-list').children.length, 3);
  for (const [index, row] of rows.entries()) {
    const item = rowItem(h, index);
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
  assert.equal(h.el('absent-count').textContent, '1');
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
  assert.equal(h.doc.activeElement, h.el('recent-tab'));
  assert.equal(h.el('active-tab').getAttribute('tabindex'), '-1');
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
  const counts = ['waiting-count', 'called-count', 'absent-count'].map(id => h.el(id).textContent);
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
    assert.deepEqual(['waiting-count', 'called-count', 'absent-count'].map(id => h.el(id).textContent), counts);
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
  assert.equal(h.el('row-menu-status').textContent, '現在状態：呼出中');
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
  for (const prefix of ['active', 'recent']) {
    assert.match(html, new RegExp(`id="${prefix}-tab"[^>]*role="tab"[^>]*aria-controls="${prefix}-panel"`));
    assert.match(html, new RegExp(`id="${prefix}-panel"[^>]*role="tabpanel"[^>]*aria-labelledby="${prefix}-tab"`));
  }
  assert.match(html, /id="reception-scroll"[^>]*tabindex="0"/);
  assert.match(html, /閲覧専用プレビュー/);
  assert.doesNotMatch(html, /onclick=|onpointer|<button[^>]*(?:seat|cancel|correction|line-send)/i);
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
  assert.equal(h.el('absent-count').textContent, '11');
  assert.match(h.el('active-list').textContent, /151/);
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
