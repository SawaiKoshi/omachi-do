import test from 'node:test';
import assert from 'node:assert/strict';

import { createStaffApp } from '../../web/staff/assets/staff.mjs';

// The browser suite deliberately provides no real browser storage, cookies, DB,
// credentials, or network. All receipts and session responses are synthetic.
const IDS = [
  'login-view', 'login-form', 'staff-token', 'login-button', 'login-message',
  'staff-view', 'logout-button', 'refresh-button', 'status-message', 'updated-at',
  'waiting-count', 'called-count', 'absent-count', 'active-list', 'recent-list',
  'detail-view', 'detail-title', 'detail-content', 'detail-close',
  'active-empty', 'recent-empty', 'security-note',
];

class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, handler) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), handler]);
  }
  removeEventListener(type, handler) {
    this.listeners.set(type, (this.listeners.get(type) ?? []).filter(item => item !== handler));
  }
  async emit(type, properties = {}) {
    const event = { type, target: this, preventDefault() {}, ...properties };
    await Promise.all((this.listeners.get(type) ?? []).map(handler => handler(event)));
    await settle();
  }
}

class Element extends Target {
  constructor(id = '') {
    super();
    this.id = id;
    this.value = '';
    this.hidden = ['staff-view', 'detail-view'].includes(id);
    this.disabled = false;
    this.dataset = {};
    this.attributes = new Map();
    this.children = [];
    this._textContent = '';
  }
  get textContent() {
    return this._textContent + this.children.map(child => child.textContent ?? child).join('');
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  set innerHTML(_value) { throw new Error('Staff/API text must never become HTML'); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  replaceChildren(...children) { this._textContent = ''; this.children = [...children]; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
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

test('a detail opened before an external list revision change is removed on refresh', async () => {
  let version = 'a'.repeat(64);
  const h = harness({ fetch: async (url) => {
    if (url === '/staff/session') return response(session());
    const current = metadata({ view_version: version });
    if (url.endsWith('/operation-context')) return response({ metadata: current, reception: reception() });
    const view = new URL(url, 'https://staff.localhost').searchParams.get('view');
    return response(listing(view === 'active' ? [reception()] : [], view, { metadata: current }));
  } });
  await h.start();
  await h.controller.openDetail(reception().id);
  assert.equal(h.el('detail-view').hidden, false);
  version = 'b'.repeat(64);
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
