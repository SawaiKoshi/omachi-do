import test from 'node:test';
import assert from 'node:assert/strict';

import { mountReception } from '../../web/static/reception.mjs';

const STORAGE_KEY = 'omachi.pending-reception.v1';
const IDS = [
  'home-view', 'home-title', 'start-button', 'back-button', 'queue-count',
  'queue-updated', 'queue-message', 'queue-list', 'called-list', 'absent-list', 'queue-empty',
  'party-form', 'entry-view', 'receipt-view', 'entry-title', 'receipt-title',
  'adults', 'children', 'total-people', 'submit-button', 'form-message',
  'request-status', 'receipt-number', 'receipt-people', 'next-button',
  'home-prediction', 'home-prediction-label', 'home-prediction-value',
  'home-prediction-updated', 'home-prediction-raw', 'home-prediction-warnings',
  'preview-two', 'preview-four', 'entry-prediction', 'entry-prediction-label',
  'entry-prediction-value', 'entry-prediction-updated', 'entry-prediction-raw',
  'entry-prediction-warnings', 'called-panel', 'absent-panel', 'queue-scroll',
];

class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) ?? [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
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
    this.disabled = false;
    this.hidden = id === 'receipt-view' || id === 'entry-view';
    this.dataset = {};
    this.attributes = new Map();
    this.children = [];
    this._textContent = '';
    this.focused = false;
    this.scrollTop = 0;
  }
  get textContent() {
    return this._textContent + this.children.map(child => child.textContent ?? child).join('');
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  set innerHTML(_value) { throw new Error('Customer/API content must use textContent, not innerHTML'); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  replaceChildren(...children) { this._textContent = ''; this.children = [...children]; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  focus(options) {
    if (this.ownerDocument?.activeElement) this.ownerDocument.activeElement.focused = false;
    this.focused = true;
    this.focusOptions = options;
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
}

class Storage {
  constructor(initial = {}) { this.values = new Map(Object.entries(initial)); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

const settle = async () => {
  // Let fetch, response.json(), and their async event handlers finish.
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
};

function receipt(overrides = {}) {
  return {
    id: '97233186-20eb-4118-ae52-64a9f52a1781',
    display_number: '20261028-007', reception_order: 7,
    received_at: '2026-10-28T12:00:00+09:00',
    adults: 1, children: 0, total_people: 1, status: 'waiting',
    called_at: null, seated_at: null, cancelled_at: null, actual_wait_minutes: null,
    snapshot: {
      captured_at: '2026-10-28T12:00:00+09:00', source: 'reception_db',
      waiting_groups: 6, ahead_groups: 6, ahead_same_size_groups: 2,
      party_size_groups: { groups_1_2: 2, groups_3_4: 2, groups_5_plus: 2 },
    },
    prediction: {
      status: 'available', minutes: 12.5, model_version: 'test-model',
      predicted_at: '2026-10-28T12:00:00+09:00',
      warnings: ['お席の状況により、案内順が前後する場合があります。'],
    },
    ...overrides,
  };
}

const response = (body = receipt(), status = 201) => ({
  ok: status >= 200 && status < 300,
  status,
  async json() { return body; },
});

const queue = (groups = [], overrides = {}) => ({
  waiting_groups: groups.length,
  groups,
  notice: 'お席の状況により、案内順が前後する場合があります。',
  ...overrides,
});

const preview = (prediction = {}, snapshot = {}) => ({
  prediction: { ...receipt().prediction, ...prediction },
  snapshot: { ...receipt().snapshot, ...snapshot },
});

function harness({ fetch: fetchImpl, queueFetch, predictFetch, storage = new Storage(), now = 0, enter = true } = {}) {
  const elements = new Map(IDS.map(id => [id, new Element(id)]));
  elements.get('queue-count').textContent = '—';
  elements.get('receipt-title').textContent = '受付完了';
  elements.get('next-button').textContent = '通知を閉じる';
  elements.get('receipt-view').append(...[
    'receipt-title', 'receipt-number', 'receipt-people', 'next-button',
  ].map(id => elements.get(id)));
  const controls = ['adults', 'children'].flatMap(person => [-1, 1].map(delta => {
    const element = new Element(`${person}-${delta}`);
    element.dataset = { person, delta: String(delta) };
    return element;
  }));
  const doc = Object.assign(new Target(), {
    visibilityState: 'visible', hidden: false, activeElement: null,
    getElementById(id) { assert.ok(elements.has(id), `Unexpected DOM id: ${id}`); return elements.get(id); },
    querySelectorAll(selector) {
      assert.equal(selector, '[data-person]');
      return controls;
    },
    createElement(tag) { return Object.assign(new Element(tag), { ownerDocument: this }); },
  });
  for (const element of [...elements.values(), ...controls]) element.ownerDocument = doc;
  const requests = [];
  const queueRequests = [];
  const predictRequests = [];
  const scrolls = [];
  const timers = new Map();
  let timerId = 0;
  let keyCount = 0;
  let time = now;
  const env = Object.assign(new Target(), {
    sessionStorage: storage,
    crypto: { randomUUID() { keyCount += 1; return `8bcad442-85bd-4c4d-90a9-${String(keyCount).padStart(12, '0')}`; } },
    AbortController,
    now() { return time; },
    scrollTo(options) { scrolls.push(options); },
    setTimeout(handler, delay) { timerId += 1; timers.set(timerId, { handler, at: time + delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      if (url === '/public/queue') {
        queueRequests.push({ url, ...options });
        return queueFetch ? queueFetch(url, options) : response(queue(), 200);
      }
      if (url === '/predict') {
        predictRequests.push({ url, ...options });
        return predictFetch ? predictFetch(url, options)
          : response(preview({ status: 'no_waiting_groups', minutes: null }), 200);
      }
      assert.equal(url, '/receptions', 'Customer UI must call only public queue, prediction, and reception endpoints');
      requests.push({ url, ...options });
      if (fetchImpl) return fetchImpl(url, options);
      const party = JSON.parse(options.body);
      return response(receipt({ ...party, total_people: party.adults + party.children }));
    },
  });
  mountReception(doc, env);
  if (enter && elements.get('entry-view').hidden && !elements.get('start-button').disabled) {
    // Most cases exercise the existing form safety contract after opening it.
    void elements.get('start-button').emit('click');
  }
  return {
    doc, env, storage, requests, queueRequests, predictRequests, scrolls, timers,
    el(id) { return elements.get(id); },
    control(person, delta) { return controls.find(item => item.dataset.person === person && item.dataset.delta === String(delta)); },
    submit() { return elements.get('party-form').emit('submit'); },
    async input(id, value) { elements.get(id).value = String(value); await elements.get(id).emit('input'); },
    async tick(milliseconds) {
      const end = time + milliseconds;
      while (true) {
        const due = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        time = due[1].at;
        timers.delete(due[0]);
        due[1].handler();
        await settle();
      }
      time = end;
      await settle();
    },
    moveClock(milliseconds) { time += milliseconds; },
  };
}

function assertReceipt(h, expected = receipt()) {
  assert.equal(h.el('entry-view').hidden, true);
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.el('receipt-view').hidden, false);
  const [date, number] = expected.display_number.split('-');
  assert.equal(h.el('receipt-title').textContent, '受付完了');
  assert.equal(h.el('receipt-number').textContent, `${date.slice(0, 4)}/${date.slice(4, 6)}/${date.slice(6, 8)} · ${number}番`);
  assert.equal(h.el('receipt-people').textContent, `${expected.total_people}人`);
  // The harness deliberately omits the removed receipt-detail elements. Any
  // remaining production access to them fails in getElementById above.
  for (const id of ['receipt-ahead', 'receipt-wait', 'receipt-warnings']) assert.equal(h.el(id), undefined);
  assert.equal(h.storage.getItem(STORAGE_KEY), null);
}

test('initial screen and count controls respect the API bounds, including child-only groups', async () => {
  const h = harness();
  assert.equal(h.el('adults').value, '1');
  assert.equal(h.el('children').value, '0');
  assert.equal(h.el('submit-button').disabled, false);
  assert.equal(h.control('children', -1).disabled, true);
  await h.control('children', -1).emit('click');
  assert.equal(h.el('children').value, '0');
  await h.control('children', 1).emit('click');
  await h.control('adults', -1).emit('click');
  assert.equal(h.el('adults').value, '0');
  assert.equal(h.el('children').value, '1');
  assert.equal(h.el('submit-button').disabled, false);
  await h.input('children', 100);
  assert.equal(h.control('children', 1).disabled, true);
  assert.equal(h.control('adults', 1).disabled, true);
  await h.submit();
  assert.deepEqual(JSON.parse(h.requests[0].body), { adults: 0, children: 100 });
});

test('successful receipt sends only public party fields and displays only its number and party size', async () => {
  const result = receipt({
    adults: 2, children: 1, total_people: 3,
    snapshot: { ...receipt().snapshot, waiting_groups: 6, ahead_groups: 2 },
    prediction: { ...receipt().prediction, warnings: ['<img src=x onerror=alert(1)>', '案内順が前後する場合があります。'] },
  });
  const h = harness({ fetch: async () => response(result) });
  await h.input('adults', 2);
  await h.input('children', 1);
  await h.submit();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, '/receptions');
  assert.equal(h.requests[0].method, 'POST');
  assert.deepEqual(JSON.parse(h.requests[0].body), { adults: 2, children: 1 });
  assert.equal(h.requests[0].headers['Content-Type'], 'application/json');
  assert.match(h.requests[0].headers['Idempotency-Key'], /^[a-f0-9-]{36}$/i);
  assert.equal(Object.keys(h.requests[0].headers).some(key => key.toLowerCase() === 'authorization'), false);
  assert.equal(h.requests[0].credentials, 'same-origin');
  assert.equal(h.requests[0].cache, 'no-store');
  assertReceipt(h, result);
});

for (const status of [200, 201]) {
  test(`HTTP ${status} is accepted as a successful reception or idempotent replay`, async () => {
    const h = harness({ fetch: async () => response(receipt(), status) });
    await h.submit();
    assertReceipt(h);
  });
}

test('response loss keeps the stored key and party fixed until an explicit retry succeeds', async () => {
  let attempts = 0;
  const h = harness({ fetch: async () => {
    attempts += 1;
    if (attempts === 1) throw new TypeError('Failed to fetch');
    return response(receipt(), 200);
  } });
  await h.submit();
  const pending = JSON.parse(h.storage.getItem(STORAGE_KEY));
  assert.deepEqual(pending, { key: h.requests[0].headers['Idempotency-Key'], adults: 1, children: 0 });
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('adults').disabled, true);
  assert.equal(h.el('children').disabled, true);
  assert.equal(h.control('children', 1).disabled, true);
  assert.equal(h.el('submit-button').disabled, false);
  assert.match(h.el('form-message').textContent + h.el('request-status').textContent, /再|確認|通信/);
  assert.equal(h.requests.length, 1);
  await h.submit();
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].headers['Idempotency-Key'], h.requests[0].headers['Idempotency-Key']);
  assert.equal(h.requests[1].body, h.requests[0].body);
  assertReceipt(h);
});

test('rapid repeated submit events produce one in-flight request', async () => {
  let finish;
  const h = harness({ fetch: () => new Promise(resolve => { finish = resolve; }) });
  const first = h.submit();
  await settle();
  const second = h.submit();
  await settle();
  assert.equal(h.requests.length, 1);
  assert.equal(h.el('submit-button').disabled, true);
  finish(response());
  await Promise.all([first, second]);
  assertReceipt(h);
});

test('a 15-second timeout aborts transport and permits retry with the original key', async () => {
  let attempts = 0;
  const h = harness({ fetch: (_url, options) => {
    attempts += 1;
    if (attempts > 1) return Promise.resolve(response(receipt(), 200));
    return new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    });
  } });
  const submitting = h.submit();
  await settle();
  await h.tick(14999);
  assert.equal(h.requests[0].signal.aborted, false);
  await h.tick(1);
  await submitting;
  assert.equal(h.requests[0].signal.aborted, true);
  assert.equal(h.el('submit-button').disabled, false);
  assert.ok(h.storage.getItem(STORAGE_KEY));
  assert.equal(h.requests.length, 1);
  await h.submit();
  assert.equal(h.requests[1].headers['Idempotency-Key'], h.requests[0].headers['Idempotency-Key']);
  assert.equal(h.requests[1].body, h.requests[0].body);
  assertReceipt(h);
});

test('HTTP 503 keeps the original key and body for retry', async () => {
  let attempts = 0;
  const h = harness({ fetch: async () => {
    attempts += 1;
    return attempts === 1 ? response({ detail: 'temporarily unavailable' }, 503) : response(receipt(), 200);
  } });
  await h.submit();
  assert.equal(h.el('receipt-view').hidden, true);
  assert.ok(h.storage.getItem(STORAGE_KEY));
  await h.submit();
  assert.equal(h.requests[1].headers['Idempotency-Key'], h.requests[0].headers['Idempotency-Key']);
  assert.equal(h.requests[1].body, h.requests[0].body);
  assertReceipt(h);
});

for (const status of ['available', 'model_unavailable', 'no_waiting_groups', 'unsupported_weekday', 'unsupported_time', 'prediction_failed']) {
  test(`receipt with null prediction (${status}) still displays a compact success notice`, async () => {
    const result = receipt({ prediction: { ...receipt().prediction, status, minutes: null } });
    const h = harness({ fetch: async () => response(result) });
    await h.submit();
    assertReceipt(h, result);
  });
}

test('a zero-minute receipt estimate does not add prediction details to the compact success notice', async () => {
  const result = receipt({ prediction: { ...receipt().prediction, minutes: 0 } });
  const h = harness({ fetch: async () => response(result) });
  await h.submit();
  assertReceipt(h, result);
});

test('long warnings, large queue snapshots, and estimates never expand the success notice', async () => {
  const warning = '長い受付時点の注意文です。'.repeat(200);
  const result = receipt({
    snapshot: { ...receipt().snapshot, waiting_groups: 123456, ahead_groups: 123456 },
    prediction: { ...receipt().prediction, minutes: 987654.5, warnings: [warning] },
  });
  const h = harness({ fetch: async () => response(result) });
  await h.submit();
  assertReceipt(h, result);
  const notice = h.el('receipt-view').textContent;
  assert.match(notice, /受付完了/);
  assert.match(notice, /007番/);
  assert.match(notice, /1人/);
  assert.doesNotMatch(notice, /123456|987654|長い受付時点|参考待ち時間|前の待ち組数/);
  assert.equal(h.el('start-button').disabled, false);
});

for (const minutes of [NaN, Infinity, -1, '12']) {
  test(`a receipt estimate (${String(minutes)}) is not used in the compact success notice`, async () => {
    const result = receipt({ prediction: { ...receipt().prediction, minutes } });
    const h = harness({ fetch: async () => response(result) });
    await h.submit();
    assertReceipt(h, result);
  });
}

for (const [adults, children] of [[0, 0], [-1, 2], [101, 0], [100, 1], [1.5, 0], ['', 1], ['abc', 1]]) {
  test(`invalid party (${JSON.stringify(adults)}, ${JSON.stringify(children)}) cannot send a request`, async () => {
    const h = harness();
    await h.input('adults', adults);
    await h.input('children', children);
    assert.equal(h.el('submit-button').disabled, true);
    await h.submit();
    assert.equal(h.requests.length, 0);
    assert.equal(h.storage.getItem(STORAGE_KEY), null);
  });
}

test('reloading a pending request restores its party and waits for explicit retry', async () => {
  const pending = { key: 'ed5646c0-7e08-4639-8e28-69432dad5b9a', adults: 2, children: 3 };
  const h = harness({ storage: new Storage({ [STORAGE_KEY]: JSON.stringify(pending) }) });
  assert.equal(h.requests.length, 0);
  assert.equal(h.el('adults').value, '2');
  assert.equal(h.el('children').value, '3');
  assert.equal(h.el('adults').disabled, true);
  assert.equal(h.el('children').disabled, true);
  await h.submit();
  assert.equal(h.requests[0].headers['Idempotency-Key'], pending.key);
  assert.deepEqual(JSON.parse(h.requests[0].body), { adults: 2, children: 3 });
});

for (const restoreOnPageShow of [true, false]) {
  test(`a cached empty form reuses another document's pending key ${restoreOnPageShow ? 'after pageshow' : 'at submit'}`, async () => {
    const storage = new Storage();
    const cached = harness({ storage });
    cached.env.crypto.randomUUID = () => 'fbd442ab-e83c-4325-a339-331548b38a50';
    const active = harness({ storage, fetch: async () => { throw new TypeError('Response lost'); } });
    await active.input('adults', 2);
    await active.input('children', 3);
    await active.submit();
    const original = JSON.parse(storage.getItem(STORAGE_KEY));
    assert.deepEqual({ adults: original.adults, children: original.children }, { adults: 2, children: 3 });

    if (restoreOnPageShow) {
      await cached.env.emit('pageshow', { persisted: true });
      assert.equal(cached.requests.length, 0);
      assert.equal(cached.el('adults').value, '2');
      assert.equal(cached.el('children').value, '3');
      assert.equal(cached.el('adults').disabled, true);
      assert.equal(cached.el('children').disabled, true);
    }
    await cached.submit();
    assert.equal(cached.requests.length, 1);
    assert.equal(cached.requests[0].headers['Idempotency-Key'], original.key);
    assert.equal(cached.requests[0].body, active.requests[0].body);
    assertReceipt(cached, receipt({ adults: 2, children: 3, total_people: 5 }));
  });
}

test('a cached pending form blocks replay when a newer document has already completed that reception', async () => {
  const storage = new Storage();
  const cached = harness({ storage, fetch: async () => { throw new TypeError('Response lost'); } });
  await cached.submit();
  const originalKey = cached.requests[0].headers['Idempotency-Key'];
  const active = harness({ storage });
  await active.submit();
  assert.equal(active.requests[0].headers['Idempotency-Key'], originalKey);
  assert.equal(storage.getItem(STORAGE_KEY), null);

  await cached.env.emit('pageshow', { persisted: true });
  assert.equal(cached.el('submit-button').disabled, true);
  assert.equal(cached.el('receipt-view').hidden, true);
  assert.match(cached.el('form-message').textContent, /スタッフ/);
  await cached.submit();
  assert.equal(cached.requests.length, 1);
  assert.equal(storage.getItem(STORAGE_KEY), null);
});

test('a conflicting stored key blocks the old document without overwriting the newer request', async () => {
  const storage = new Storage();
  const cached = harness({ storage, fetch: async () => { throw new TypeError('Response lost'); } });
  await cached.submit();
  const newerPending = JSON.stringify({ key: 'bba76fe8-9348-49ea-b024-26d3b4f547dc', adults: 2, children: 1 });
  storage.setItem(STORAGE_KEY, newerPending);
  await cached.env.emit('pageshow', { persisted: true });
  assert.equal(cached.el('submit-button').disabled, true);
  await cached.submit();
  assert.equal(cached.requests.length, 1);
  assert.equal(storage.getItem(STORAGE_KEY), newerPending);
});

for (const value of ['{broken', '{}', JSON.stringify({ key: '', adults: 1, children: 0 }), JSON.stringify({ key: 'valid-key', adults: 0, children: 0 })]) {
  test(`corrupt pending storage blocks new submissions (${value})`, async () => {
    const h = harness({ storage: new Storage({ [STORAGE_KEY]: value }) });
    assert.equal(h.el('submit-button').disabled, true);
    await h.submit();
    assert.equal(h.requests.length, 0);
    assert.equal(h.storage.getItem(STORAGE_KEY), value);
  });
}

test('unavailable storage blocks requests before sending', async () => {
  const storage = new Storage();
  storage.getItem = () => { throw new Error('Storage is disabled'); };
  const h = harness({ storage });
  assert.equal(h.el('submit-button').disabled, true);
  await h.submit();
  assert.equal(h.requests.length, 0);
});

test('a failure to persist the key blocks the request', async () => {
  const storage = new Storage();
  storage.setItem = () => { throw new Error('Quota exceeded'); };
  const h = harness({ storage });
  await h.submit();
  assert.equal(h.requests.length, 0);
  assert.equal(h.el('receipt-view').hidden, true);
  assert.match(h.el('form-message').textContent + h.el('request-status').textContent, /保存|保持|利用|確認/);
});

test('a key-generation failure blocks the request before any network call', async () => {
  const h = harness();
  h.env.crypto.randomUUID = () => { throw new Error('Secure randomness unavailable'); };
  await h.submit();
  assert.equal(h.requests.length, 0);
  assert.equal(h.el('submit-button').disabled, true);
});

test('a malformed successful response retains its key and displays no receipt', async () => {
  let attempts = 0;
  const h = harness({ fetch: async () => {
    attempts += 1;
    return response(attempts === 1 ? receipt({ adults: 5, total_people: 5 }) : receipt());
  } });
  await h.submit();
  assert.equal(h.el('receipt-view').hidden, true);
  assert.ok(h.storage.getItem(STORAGE_KEY));
  await h.submit();
  assert.equal(h.requests[1].headers['Idempotency-Key'], h.requests[0].headers['Idempotency-Key']);
  assertReceipt(h);
});

for (const [label, invalid] of [
  ['missing snapshot', { snapshot: undefined }],
  ['negative waiting groups', { snapshot: { waiting_groups: -1 } }],
  ['noninteger waiting groups', { snapshot: { waiting_groups: 1.5 } }],
  ['missing prediction', { prediction: undefined }],
  ['missing warnings', { prediction: {} }],
  ['nontext warnings', { prediction: { warnings: [7] } }],
]) {
  test(`compact notice retains receipt validation and the retry key for ${label}`, async () => {
    let attempts = 0;
    const h = harness({ fetch: async () => response(++attempts === 1 ? receipt(invalid) : receipt(), 200) });
    await h.submit();
    assert.equal(h.el('receipt-view').hidden, true);
    assert.equal(h.el('receipt-number').textContent, '');
    assert.equal(h.el('entry-view').hidden, false);
    assert.ok(h.storage.getItem(STORAGE_KEY));
    await h.submit();
    assert.equal(h.requests[1].headers['Idempotency-Key'], h.requests[0].headers['Idempotency-Key']);
    assert.equal(h.requests[1].body, h.requests[0].body);
    assertReceipt(h);
  });
}

test('storage cleanup failure preserves the pending key for a safe retry', async () => {
  const storage = new Storage();
  storage.removeItem = () => { throw new Error('Storage unavailable'); };
  const h = harness({ storage });
  await h.submit();
  assert.equal(h.el('receipt-view').hidden, true);
  assert.ok(storage.getItem(STORAGE_KEY));
  await h.submit();
  assert.equal(h.requests[1].headers['Idempotency-Key'], h.requests[0].headers['Idempotency-Key']);
});

test('the next customer can start immediately and uses a fresh key', async () => {
  const h = harness();
  await h.input('adults', 3);
  await h.submit();
  const firstKey = h.requests[0].headers['Idempotency-Key'];
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.el('start-button').disabled, false);
  await h.el('next-button').emit('click');
  assert.equal(h.el('entry-view').hidden, true);
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('adults').value, '1');
  assert.equal(h.el('children').value, '0');
  assert.equal(h.el('receipt-number').textContent, '');
  assert.equal(h.el('receipt-people').textContent, '');
  await h.el('start-button').emit('click');
  await h.submit();
  assert.notEqual(h.requests[1].headers['Idempotency-Key'], firstKey);
  assert.deepEqual(JSON.parse(h.requests[1].body), { adults: 1, children: 0 });
});

test('submission events from a completed screen cannot create another reception', async () => {
  const h = harness();
  await h.submit();
  await h.submit();
  assert.equal(h.requests.length, 1);
  assertReceipt(h);
});

test('receipt notification clears after 30 seconds while home is immediately usable', async () => {
  const h = harness();
  await h.submit();
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.el('start-button').disabled, false);
  await h.tick(29999);
  assert.equal(h.el('receipt-view').hidden, false);
  await h.tick(1);
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('entry-view').hidden, true);
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.el('receipt-number').textContent, '');
});

test('leaving the page clears a successful receipt before a cached page can restore it', async () => {
  const h = harness();
  await h.submit();
  await h.env.emit('pagehide', { persisted: true });
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('receipt-number').textContent, '');
});

test('hiding the tab clears the receipt immediately', async () => {
  const h = harness();
  await h.submit();
  h.doc.visibilityState = 'hidden';
  await h.doc.emit('visibilitychange');
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('receipt-number').textContent, '');
});

test('a response arriving after the tab was hidden never leaves a receipt visible', async () => {
  let finish;
  const h = harness({ fetch: () => new Promise(resolve => { finish = resolve; }) });
  const submitting = h.submit();
  await settle();
  h.doc.visibilityState = 'hidden';
  await h.doc.emit('visibilitychange');
  finish(response());
  await submitting;
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('receipt-number').textContent, '');
  assert.equal(h.storage.getItem(STORAGE_KEY), null);
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.el('start-button').disabled, false);
});

test('a successful response arriving after pagehide keeps the pending key for the restored document', async () => {
  let finish;
  let attempts = 0;
  const h = harness({ fetch: () => {
    attempts += 1;
    return attempts === 1 ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(response(receipt(), 200));
  } });
  const submitting = h.submit();
  await settle();
  const originalPending = h.storage.getItem(STORAGE_KEY);
  await h.env.emit('pagehide', { persisted: true });
  finish(response());
  await submitting;
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('receipt-number').textContent, '');
  assert.equal(h.storage.getItem(STORAGE_KEY), originalPending);
  assert.equal(h.requests.length, 1);
  await h.env.emit('pageshow', { persisted: true });
  assert.equal(h.requests.length, 1);
  await h.submit();
  assert.equal(h.requests[1].headers['Idempotency-Key'], h.requests[0].headers['Idempotency-Key']);
  assert.equal(h.requests[1].body, h.requests[0].body);
  assertReceipt(h);
});

test('returning from a suspended page clears an expired receipt even if its timer did not run', async () => {
  const h = harness();
  await h.submit();
  h.moveClock(61000);
  await h.env.emit('pageshow', { persisted: true });
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('receipt-number').textContent, '');
});

test('pagehide preserves a pending request so navigation cannot lose its idempotency key', async () => {
  const h = harness({ fetch: async () => { throw new TypeError('Failed to fetch'); } });
  await h.submit();
  const pending = h.storage.getItem(STORAGE_KEY);
  await h.env.emit('pagehide', { persisted: true });
  assert.equal(h.storage.getItem(STORAGE_KEY), pending);
  assert.equal(h.el('adults').disabled, true);
});

test('dashboard starts at home and opens the existing party form on demand', async () => {
  const h = harness({ enter: false });
  await settle();
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.el('entry-view').hidden, true);
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('queue-count').textContent, '0');
  assert.equal(h.queueRequests.length, 1);
  assert.equal(h.requests.length, 0);
  assert.equal(h.queueRequests[0].method, 'GET');
  assert.equal(h.queueRequests[0].credentials, 'same-origin');
  assert.equal(h.queueRequests[0].cache, 'no-store');
  assert.equal(h.queueRequests[0].headers?.Authorization, undefined);
  assert.equal(h.queueRequests[0].headers?.['Idempotency-Key'], undefined);

  await h.el('start-button').emit('click');
  assert.equal(h.el('home-view').hidden, true);
  assert.equal(h.el('entry-view').hidden, false);
  assert.equal(h.el('adults').value, '1');
  assert.equal(h.el('children').value, '0');
  await h.input('adults', 4);
  await h.el('back-button').emit('click');
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.el('entry-view').hidden, true);
  assert.equal(h.requests.length, 0);
  assert.equal(h.storage.getItem(STORAGE_KEY), null);
  await h.el('start-button').emit('click');
  assert.equal(h.el('adults').value, '1');
});

test('public queue renders all active parties in reception order and separates called and absent summaries', async () => {
  const groups = [
    { display_number: '20261027-023', total_people: 2, status: 'absent' },
    { display_number: '20261028-001', total_people: 5, status: 'called' },
    { display_number: '20261028-002', total_people: 1, status: 'waiting' },
  ];
  const h = harness({ enter: false, queueFetch: async () => response(queue(groups), 200) });
  await settle();
  assert.equal(h.el('queue-count').textContent, '3');
  assert.deepEqual(h.el('queue-list').children.map(row => row.dataset.number), groups.map(group => group.display_number));
  assert.match(h.el('queue-list').textContent, /023番/);
  assert.match(h.el('queue-list').textContent, /2026\/10\/27受付/);
  assert.match(h.el('queue-list').textContent, /001番/);
  assert.match(h.el('queue-list').textContent, /2026\/10\/28受付/);
  assert.match(h.el('queue-list').textContent, /5人/);
  assert.match(h.el('called-list').textContent, /001番/);
  assert.doesNotMatch(h.el('called-list').textContent, /023番|002番/);
  assert.match(h.el('absent-list').textContent, /023番/);
  assert.doesNotMatch(h.el('absent-list').textContent, /001番|002番/);
  assert.equal(h.el('queue-empty').hidden, true);
  assert.match(h.el('queue-updated').textContent, /最終更新/);
});

test('polling reflects new receptions, staff status changes, and removals without a staff endpoint', async () => {
  let state = queue([{ display_number: '20261028-001', total_people: 2, status: 'waiting' }]);
  const h = harness({ enter: false, queueFetch: async () => response(state, 200) });
  await settle();
  assert.equal(h.el('queue-count').textContent, '1');
  state = queue([
    { display_number: '20261028-001', total_people: 2, status: 'called' },
    { display_number: '20261028-002', total_people: 3, status: 'waiting' },
  ]);
  await h.tick(5000);
  assert.equal(h.el('queue-count').textContent, '2');
  assert.match(h.el('called-list').textContent, /001番/);
  state = queue([{ display_number: '20261028-002', total_people: 3, status: 'absent' }]);
  await h.tick(5000);
  assert.equal(h.el('queue-count').textContent, '1');
  assert.doesNotMatch(h.el('queue-list').textContent, /001番/);
  assert.match(h.el('absent-list').textContent, /002番/);
  state = queue();
  await h.tick(5000);
  assert.equal(h.el('queue-count').textContent, '0');
  assert.equal(h.el('queue-list').children.length, 0);
  assert.equal(h.el('queue-empty').hidden, false);
  assert.equal(h.queueRequests.length, 4);
  assert.equal(h.requests.length, 0);
  assert.ok(h.queueRequests.every(request => request.url === '/public/queue'));
});

test('an initial public queue failure displays unknown count rather than zero and retries', async () => {
  let attempts = 0;
  const h = harness({ enter: false, queueFetch: async () => {
    attempts += 1;
    if (attempts === 1) throw new TypeError('Offline');
    return response(queue(), 200);
  } });
  await settle();
  assert.equal(h.el('queue-count').textContent, '—');
  assert.match(h.el('queue-message').textContent, /更新できません/);
  assert.equal(h.el('start-button').disabled, false);
  await h.tick(5000);
  assert.equal(h.el('queue-count').textContent, '0');
  assert.equal(h.el('queue-message').textContent, '');
});

test('a failed public refresh preserves the last queue with a stale-data message', async () => {
  let attempts = 0;
  const h = harness({ enter: false, queueFetch: async () => {
    attempts += 1;
    return attempts === 1
      ? response(queue([{ display_number: '20261028-004', total_people: 4, status: 'waiting' }]), 200)
      : response({ detail: 'busy' }, 503);
  } });
  await settle();
  const lastUpdated = h.el('queue-updated').textContent;
  await h.tick(5000);
  assert.equal(h.el('queue-count').textContent, '1');
  assert.match(h.el('queue-list').textContent, /004番/);
  assert.match(h.el('queue-message').textContent, /最新ではありません/);
  assert.equal(h.el('queue-updated').textContent, lastUpdated);
  assert.equal(h.el('receipt-view').hidden, true);
});

for (const invalid of [
  { waiting_groups: 1, groups: [] },
  { waiting_groups: 1, groups: [{ display_number: '20261028-001', total_people: 0, status: 'waiting' }] },
  { waiting_groups: 1, groups: [{ display_number: '20261028-001', total_people: 2, status: 'seated' }] },
  { waiting_groups: 2, groups: [
    { display_number: '20261028-001', total_people: 2, status: 'waiting' },
    { display_number: '20261028-001', total_people: 2, status: 'waiting' },
  ] },
]) {
  test(`malformed public queue is never shown as current data (${JSON.stringify(invalid)})`, async () => {
    const h = harness({ enter: false, queueFetch: async () => response(queue([], invalid), 200) });
    await settle();
    assert.equal(h.el('queue-count').textContent, '—');
    assert.equal(h.el('queue-list').children.length, 0);
    assert.match(h.el('queue-message').textContent, /更新できません/);
  });
}

test('public queue rendering treats API strings as text', async () => {
  const malicious = '<img src=x onerror=alert(1)>';
  const h = harness({ enter: false, queueFetch: async () => response(queue([
    { display_number: malicious, total_people: 2, status: 'called' },
  ]), 200) });
  await settle();
  assert.match(h.el('queue-list').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(h.el('called-list').textContent, /<img src=x onerror=alert\(1\)>/);
});

test('a slow public request is not overlapped and polling resumes five seconds after completion', async () => {
  let finish;
  const h = harness({ enter: false, queueFetch: () => new Promise(resolve => { finish = resolve; }) });
  await h.tick(10000);
  assert.equal(h.queueRequests.length, 1);
  finish(response(queue(), 200));
  await settle();
  await h.tick(4999);
  assert.equal(h.queueRequests.length, 1);
  await h.tick(1);
  assert.equal(h.queueRequests.length, 2);
});

test('a public request times out after fifteen seconds and resumes polling', async () => {
  const h = harness({ enter: false, queueFetch: (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
  }) });
  await h.tick(14999);
  assert.equal(h.queueRequests[0].signal.aborted, false);
  await h.tick(1);
  assert.equal(h.queueRequests[0].signal.aborted, true);
  assert.match(h.el('queue-message').textContent, /更新できません/);
  await h.tick(5000);
  assert.equal(h.queueRequests.length, 2);
});

test('success returns home immediately, refreshes its queue, and highlights the new number for thirty seconds', async () => {
  let registered = false;
  const h = harness({
    fetch: async () => { registered = true; return response(); },
    queueFetch: async () => response(queue(registered ? [
      { display_number: '20261028-007', total_people: 1, status: 'waiting' },
    ] : []), 200),
  });
  await settle();
  await h.submit();
  assertReceipt(h);
  assert.deepEqual(h.scrolls.at(-1), { top: 0, left: 0, behavior: 'auto' });
  assert.equal(h.el('receipt-title').focused, true);
  assert.deepEqual(h.el('receipt-title').focusOptions, { preventScroll: true });
  assert.equal(h.el('start-button').disabled, false);
  assert.equal(h.queueRequests.length, 2);
  assert.equal(h.el('queue-count').textContent, '1');
  assert.match(h.el('queue-list').children[0].className, /recent/);
  await h.tick(30000);
  assert.equal(h.el('receipt-view').hidden, true);
  assert.doesNotMatch(h.el('queue-list').children[0].className, /recent/);
});

test('consecutive arrivals retain independent number highlights and display the latest receipt', async () => {
  const groups = [];
  const h = harness({
    fetch: async () => {
      const display_number = `20261028-${String(groups.length + 7).padStart(3, '0')}`;
      groups.push({ display_number, total_people: 1, status: 'waiting' });
      return response(receipt({ display_number }));
    },
    queueFetch: async () => response(queue([...groups]), 200),
  });
  await h.submit();
  await h.tick(10000);
  await h.el('start-button').emit('click');
  await h.submit();
  assert.equal(h.requests.length, 2);
  assert.notEqual(h.requests[0].headers['Idempotency-Key'], h.requests[1].headers['Idempotency-Key']);
  assertReceipt(h, receipt({ display_number: '20261028-008' }));
  assert.equal(h.el('start-button').disabled, false);
  assert.equal(h.el('queue-list').children.length, 2);
  for (const row of h.el('queue-list').children) assert.match(row.className, /recent/);

  await h.tick(20000);
  assert.doesNotMatch(h.el('queue-list').children[0].className, /recent/);
  assert.match(h.el('queue-list').children[1].className, /recent/);
  assert.equal(h.el('receipt-view').hidden, false);
  assert.match(h.el('receipt-number').textContent, /008番/);
  await h.tick(10000);
  for (const row of h.el('queue-list').children) assert.doesNotMatch(row.className, /recent/);
  assert.equal(h.el('receipt-view').hidden, true);
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.el('start-button').disabled, false);
});

test('an old public response arriving after registration cannot replace the refreshed queue', async () => {
  let finishOld;
  let attempts = 0;
  const h = harness({ queueFetch: () => {
    attempts += 1;
    if (attempts === 1) return new Promise(resolve => { finishOld = resolve; });
    return response(queue([{ display_number: '20261028-007', total_people: 1, status: 'waiting' }]), 200);
  } });
  await h.submit();
  assert.equal(h.queueRequests[0].signal.aborted, true);
  finishOld(response(queue(), 200));
  await settle();
  assert.equal(h.el('queue-count').textContent, '1');
  assert.match(h.el('queue-list').textContent, /007番/);
  assertReceipt(h);
});

test('notification expiry cannot erase the next customer selection', async () => {
  const h = harness();
  await h.submit();
  await h.tick(1000);
  await h.el('start-button').emit('click');
  assert.equal(h.el('receipt-view').hidden, true);
  await h.input('adults', 4);
  await h.input('children', 2);
  await h.tick(29000);
  assert.equal(h.el('entry-view').hidden, false);
  assert.equal(h.el('adults').value, '4');
  assert.equal(h.el('children').value, '2');
  assert.equal(h.el('receipt-number').textContent, '');
});

test('an untouched unsent party form returns home after sixty seconds without creating a reception', async () => {
  const h = harness();
  await h.input('adults', 3);
  await h.tick(59999);
  assert.equal(h.el('entry-view').hidden, false);
  await h.tick(1);
  assert.equal(h.el('entry-view').hidden, true);
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.requests.length, 0);
  assert.equal(h.storage.getItem(STORAGE_KEY), null);
  await h.el('start-button').emit('click');
  assert.equal(h.el('adults').value, '1');
});

test('party count interaction renews the unsent idle deadline', async () => {
  const h = harness();
  await h.tick(59000);
  await h.control('children', 1).emit('click');
  await h.tick(59000);
  assert.equal(h.el('entry-view').hidden, false);
  assert.equal(h.el('children').value, '1');
  await h.tick(1000);
  assert.equal(h.el('home-view').hidden, false);
});

test('restoring a suspended unsent form checks elapsed idle time even when its timer did not run', async () => {
  const h = harness();
  await h.input('adults', 5);
  h.moveClock(61000);
  await h.env.emit('pageshow', { persisted: true });
  assert.equal(h.el('home-view').hidden, false);
  assert.equal(h.el('entry-view').hidden, true);
  assert.equal(h.requests.length, 0);
  assert.equal(h.storage.getItem(STORAGE_KEY), null);
});

test('an uncertain reception cannot be discarded by back, idle timeout, or start', async () => {
  const h = harness({ fetch: async () => { throw new TypeError('Lost response'); } });
  await h.submit();
  const original = h.storage.getItem(STORAGE_KEY);
  assert.equal(h.el('back-button').disabled, true);
  await h.el('back-button').emit('click');
  await h.tick(120000);
  assert.equal(h.el('entry-view').hidden, false);
  assert.equal(h.el('home-view').hidden, true);
  assert.equal(h.storage.getItem(STORAGE_KEY), original);
  await h.el('start-button').emit('click');
  assert.equal(h.storage.getItem(STORAGE_KEY), original);
  assert.equal(h.el('adults').disabled, true);
  assert.equal(h.requests.length, 1);
});

test('hiding the page clears notifications and highlights, pauses polling, and resumes with a fresh public request', async () => {
  const h = harness({ queueFetch: async () => response(queue([
    { display_number: '20261028-007', total_people: 1, status: 'waiting' },
  ]), 200) });
  await h.submit();
  assert.match(h.el('queue-list').children[0].className, /recent/);
  h.doc.visibilityState = 'hidden';
  await h.doc.emit('visibilitychange');
  assert.equal(h.el('receipt-view').hidden, true);
  assert.doesNotMatch(h.el('queue-list').children[0].className, /recent/);
  const count = h.queueRequests.length;
  await h.tick(30000);
  assert.equal(h.queueRequests.length, count);
  h.doc.visibilityState = 'visible';
  await h.doc.emit('visibilitychange');
  assert.equal(h.queueRequests.length, count + 1);
});

test('hiding an unsent form returns home, while a pending form and its key remain available', async () => {
  const unsent = harness();
  await unsent.input('adults', 3);
  unsent.doc.visibilityState = 'hidden';
  await unsent.doc.emit('visibilitychange');
  assert.equal(unsent.el('home-view').hidden, false);
  assert.equal(unsent.el('entry-view').hidden, true);
  const pending = harness({ fetch: async () => { throw new TypeError('Lost response'); } });
  await pending.submit();
  const original = pending.storage.getItem(STORAGE_KEY);
  pending.doc.visibilityState = 'hidden';
  await pending.doc.emit('visibilitychange');
  assert.equal(pending.storage.getItem(STORAGE_KEY), original);
  assert.equal(pending.el('entry-view').hidden, false);
});

const occupiedQueue = () => response(queue([
  { display_number: '20261028-001', total_people: 2, status: 'waiting' },
]), 200);

test('home previews two adults independently of a slow queue without registering or creating a key', async () => {
  const h = harness({
    enter: false, queueFetch: () => new Promise(() => {}),
    predictFetch: async () => response(preview(), 200),
  });
  await settle();
  assert.equal(h.predictRequests.length, 1);
  const request = h.predictRequests[0];
  assert.equal(request.url, '/predict');
  assert.equal(request.method, 'POST');
  assert.deepEqual(JSON.parse(request.body), { adults: 2, children: 0 });
  assert.equal(request.credentials, 'same-origin');
  assert.equal(request.cache, 'no-store');
  assert.equal(request.headers['Content-Type'], 'application/json');
  assert.equal(Object.keys(request.headers).some(key => ['authorization', 'idempotency-key'].includes(key.toLowerCase())), false);
  assert.match(h.el('home-prediction-label').textContent, /大人2/);
  assert.equal(h.el('home-prediction-value').textContent, '約13分');
  assert.match(h.el('home-prediction-raw').textContent, /12\.5/);
  assert.equal(h.el('home-prediction-raw').hidden, false);
  assert.match(h.el('home-prediction-updated').textContent, /更新|取得|予測/);
  assert.equal(h.requests.length, 0);
  assert.equal(h.storage.getItem(STORAGE_KEY), null);
  assert.equal(h.el('start-button').disabled, false);
});

test('four-adult estimate is fetched only when selected and switching back reuses the fresh two-adult estimate', async () => {
  const h = harness({ enter: false, queueFetch: occupiedQueue,
    predictFetch: async (_url, options) => response(preview({ minutes: JSON.parse(options.body).adults * 10 }), 200),
  });
  await settle();
  assert.equal(h.predictRequests.length, 1);
  assert.equal(h.el('home-prediction-value').textContent, '約20分');
  await h.el('preview-four').emit('click');
  assert.equal(h.predictRequests.length, 2);
  assert.deepEqual(JSON.parse(h.predictRequests[1].body), { adults: 4, children: 0 });
  assert.match(h.el('home-prediction-label').textContent, /大人4/);
  assert.equal(h.el('home-prediction-value').textContent, '約40分');
  await h.el('preview-two').emit('click');
  assert.equal(h.predictRequests.length, 2);
  assert.equal(h.el('home-prediction-value').textContent, '約20分');
});

test('five-second queue polling does not infer again before the independent sixty-second preview refresh', async () => {
  const h = harness({ enter: false, queueFetch: occupiedQueue, predictFetch: async () => response(preview(), 200) });
  await settle();
  await h.tick(59999);
  assert.equal(h.queueRequests.length, 12);
  assert.equal(h.predictRequests.length, 1);
  await h.tick(1);
  assert.equal(h.queueRequests.length, 13);
  assert.equal(h.predictRequests.length, 2);
  assert.equal(h.requests.length, 0);
});

test('person selection debounces rapid edits and previews the complete selected party without blocking submit', async () => {
  const h = harness({ queueFetch: occupiedQueue, predictFetch: async () => response(preview(), 200) });
  await settle();
  await h.input('adults', 3);
  await h.input('children', 2);
  assert.equal(h.el('submit-button').disabled, false);
  await h.tick(399);
  assert.equal(h.predictRequests.length, 1);
  await h.tick(1);
  assert.equal(h.predictRequests.length, 2);
  assert.deepEqual(JSON.parse(h.predictRequests[1].body), { adults: 3, children: 2 });
  assert.match(h.el('entry-prediction-label').textContent, /3/);
  assert.match(h.el('entry-prediction-label').textContent, /2/);
  assert.equal(h.el('entry-prediction-value').textContent, '約13分');
  assert.equal(h.requests.length, 0);
  assert.equal(h.storage.getItem(STORAGE_KEY), null);
});

test('the home and selection previews share cache by adult and child counts rather than total people', async () => {
  const h = harness({ enter: false, queueFetch: occupiedQueue, predictFetch: async () => response(preview(), 200) });
  await settle();
  await h.el('start-button').emit('click');
  await h.input('adults', 2);
  await h.tick(400);
  assert.equal(h.predictRequests.length, 1, 'Two-adult preview from home should be reused');
  assert.equal(h.el('entry-prediction-value').textContent, '約13分');
  await h.input('adults', 0);
  await h.input('children', 2);
  await h.tick(400);
  assert.equal(h.predictRequests.length, 2, 'Two children require their own prediction');
  assert.deepEqual(JSON.parse(h.predictRequests[1].body), { adults: 0, children: 2 });
});

test('repeated requests for the same in-flight home prediction are deduplicated', async () => {
  let finish;
  const h = harness({ enter: false, queueFetch: occupiedQueue,
    predictFetch: () => new Promise(resolve => { finish = resolve; }),
  });
  await h.el('preview-two').emit('click');
  await h.el('preview-two').emit('click');
  assert.equal(h.predictRequests.length, 1);
  assert.equal(h.el('start-button').disabled, false);
  finish(response(preview(), 200));
  await settle();
  assert.equal(h.el('home-prediction-value').textContent, '約13分');
});

test('previous-party response cannot overwrite a newer party prediction', async () => {
  const completions = new Map();
  const h = harness({ queueFetch: occupiedQueue, predictFetch: (_url, options) => {
    const { adults, children } = JSON.parse(options.body);
    if (adults === 2 && children === 0) return response(preview(), 200);
    return new Promise(resolve => completions.set(`${adults}:${children}`, resolve));
  } });
  await h.tick(400);
  await h.input('adults', 3);
  await h.tick(400);
  assert.equal(h.predictRequests.length, 3);
  completions.get('3:0')(response(preview({ minutes: 25 }), 200));
  await settle();
  assert.equal(h.el('entry-prediction-value').textContent, '約25分');
  completions.get('1:0')(response(preview({ minutes: 90 }), 200));
  await settle();
  assert.equal(h.el('entry-prediction-value').textContent, '約25分');
  assert.match(h.el('entry-prediction-label').textContent, /3/);
});

test('changing the selected party clears its old numeric estimate during the debounce and request', async () => {
  const h = harness({ queueFetch: occupiedQueue, predictFetch: (_url, options) => {
    return JSON.parse(options.body).adults === 3 ? new Promise(() => {}) : response(preview(), 200);
  } });
  await h.tick(400);
  assert.equal(h.el('entry-prediction-value').textContent, '約13分');
  await h.input('adults', 3);
  assert.doesNotMatch(h.el('entry-prediction-value').textContent, /13分/);
  await h.tick(400);
  assert.doesNotMatch(h.el('entry-prediction-value').textContent, /13分/);
  assert.equal(h.el('submit-button').disabled, false);
});

for (const status of ['model_unavailable', 'no_waiting_groups', 'unsupported_weekday', 'unsupported_time', 'prediction_failed']) {
  test(`preview unavailable status ${status} does not become zero minutes and never prevents registration`, async () => {
    const h = harness({ enter: false, queueFetch: occupiedQueue,
      predictFetch: async () => response(preview({ status, minutes: null }), 200),
    });
    await settle();
    assert.match(h.el('home-prediction-value').textContent, /現在、参考待ち時間を表示できません/);
    assert.doesNotMatch(h.el('home-prediction-value').textContent, /0分/);
    await h.el('start-button').emit('click');
    await h.tick(400);
    assert.match(h.el('entry-prediction-value').textContent, /現在、参考待ち時間を表示できません/);
    assert.doesNotMatch(h.el('entry-prediction-value').textContent, /0分/);
    assert.equal(h.el('submit-button').disabled, false);
    await h.submit();
    assertReceipt(h);
  });
}

for (const minutes of [null, NaN, Infinity, -1, '12']) {
  test(`malformed available preview ${String(minutes)} is unavailable instead of a fabricated number`, async () => {
    const h = harness({ enter: false, queueFetch: occupiedQueue,
      predictFetch: async () => response(preview({ minutes }), 200),
    });
    await settle();
    assert.match(h.el('home-prediction-value').textContent, /現在、参考待ち時間を表示できません/);
    assert.equal(h.el('start-button').disabled, false);
    assert.equal(h.requests.length, 0);
  });
}

test('an unsupported preview status with a misleading numeric value remains unavailable', async () => {
  const h = harness({ enter: false, queueFetch: occupiedQueue,
    predictFetch: async () => response(preview({ status: 'unsupported_time', minutes: 15 }), 200),
  });
  await settle();
  assert.match(h.el('home-prediction-value').textContent, /現在、参考待ち時間を表示できません/);
  assert.doesNotMatch(h.el('home-prediction-value').textContent, /15分/);
});

for (const status of [422, 503]) {
  test(`prediction HTTP ${status} does not block the form or falsely complete a reception`, async () => {
    const h = harness({ queueFetch: occupiedQueue,
      predictFetch: async () => response({ detail: 'Cannot predict' }, status),
    });
    await h.tick(400);
    assert.match(h.el('entry-prediction-value').textContent, /参考待ち時間を表示できません/);
    assert.equal(h.el('submit-button').disabled, false);
    assert.equal(h.el('receipt-view').hidden, true);
    assert.equal(h.storage.getItem(STORAGE_KEY), null);
    await h.submit();
    assertReceipt(h);
    assert.equal(h.requests.length, 1);
  });
}

test('a prediction network failure does not change reception retry safety', async () => {
  let attempts = 0;
  const h = harness({ queueFetch: occupiedQueue,
    predictFetch: async () => { throw new TypeError('Predict response lost'); },
    fetch: async () => {
      attempts += 1;
      if (attempts === 1) throw new TypeError('Registration response lost');
      return response(receipt(), 200);
    },
  });
  await h.tick(400);
  assert.match(h.el('entry-prediction-value').textContent, /参考待ち時間を表示できません/);
  await h.submit();
  assert.equal(h.el('receipt-view').hidden, true);
  assert.ok(h.storage.getItem(STORAGE_KEY));
  await h.submit();
  assert.equal(h.requests[0].headers['Idempotency-Key'], h.requests[1].headers['Idempotency-Key']);
  assertReceipt(h);
});

test('a slow preview does not delay reception, immediate home return, or the next customer', async () => {
  const h = harness({ queueFetch: occupiedQueue, predictFetch: () => new Promise(() => {}) });
  await h.tick(400);
  assert.equal(h.el('submit-button').disabled, false);
  await h.submit();
  assertReceipt(h);
  await h.el('start-button').emit('click');
  await h.submit();
  assert.equal(h.requests.length, 2);
  assert.notEqual(h.requests[0].headers['Idempotency-Key'], h.requests[1].headers['Idempotency-Key']);
  assertReceipt(h);
});

test('invalid party changes never send a preview request for zero people', async () => {
  const h = harness({ queueFetch: occupiedQueue, predictFetch: async () => response(preview(), 200) });
  await h.input('adults', 0);
  await h.tick(400);
  assert.equal(h.predictRequests.length, 1);
  assert.equal(h.el('submit-button').disabled, true);
  await h.input('children', 1);
  await h.tick(400);
  assert.deepEqual(JSON.parse(h.predictRequests.at(-1).body), { adults: 0, children: 1 });
  assert.equal(h.el('submit-button').disabled, false);
});

test('preview warnings are preserved as text including model caveats and API strings', async () => {
  const warnings = ['<img src=x onerror=alert(1)>', '待ち組数や空席は計算に含めていません。', '案内順が前後する場合があります。'];
  const h = harness({ enter: false, queueFetch: occupiedQueue,
    predictFetch: async () => response(preview({ warnings }), 200),
  });
  await settle();
  for (const warning of warnings) assert.ok(h.el('home-prediction-warnings').textContent.includes(warning));
  await h.el('start-button').emit('click');
  await h.tick(400);
  for (const warning of warnings) assert.ok(h.el('entry-prediction-warnings').textContent.includes(warning));
});

test('preview timeout cancels transport without locking registration', async () => {
  const h = harness({ enter: false, queueFetch: occupiedQueue,
    predictFetch: (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }),
  });
  await h.tick(14999);
  assert.equal(h.predictRequests[0].signal.aborted, false);
  await h.tick(1);
  assert.equal(h.predictRequests[0].signal.aborted, true);
  assert.match(h.el('home-prediction-value').textContent, /参考待ち時間を表示できません/);
  assert.equal(h.el('start-button').disabled, false);
});

test('backgrounding aborts preview requests and prevents periodic prediction while hidden', async () => {
  const h = harness({ enter: false, queueFetch: occupiedQueue,
    predictFetch: (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }),
  });
  h.doc.visibilityState = 'hidden';
  await h.doc.emit('visibilitychange');
  assert.equal(h.predictRequests[0].signal.aborted, true);
  const count = h.predictRequests.length;
  await h.tick(120000);
  assert.equal(h.predictRequests.length, count);
  h.doc.visibilityState = 'visible';
  await h.doc.emit('visibilitychange');
  assert.equal(h.predictRequests.length, count + 1);
});

test('unchanged five-second queue updates keep rows, list scroll position, and active controls stable', async () => {
  const h = harness({ enter: false, queueFetch: occupiedQueue });
  await settle();
  const row = h.el('queue-list').children[0];
  h.el('queue-scroll').scrollTop = 48;
  h.el('start-button').focus();
  await h.tick(5000);
  assert.equal(h.el('queue-list').children[0], row);
  assert.equal(h.el('queue-scroll').scrollTop, 48);
  assert.equal(h.el('start-button').focused, true);
  assert.equal(h.doc.activeElement, h.el('start-button'));
});

test('called and absent panels become compact when empty and reappear for staff state changes', async () => {
  let state = queue();
  const h = harness({ enter: false, queueFetch: async () => response(state, 200) });
  await settle();
  assert.equal(h.el('called-panel').hidden, true);
  assert.equal(h.el('absent-panel').hidden, true);
  state = queue([
    { display_number: '20261028-001', total_people: 2, status: 'called' },
    { display_number: '20261027-001', total_people: 3, status: 'absent' },
  ]);
  await h.tick(5000);
  assert.equal(h.el('called-panel').hidden, false);
  assert.equal(h.el('absent-panel').hidden, false);
  assert.match(h.el('called-list').textContent, /2026\/10\/28受付/);
  assert.match(h.el('absent-list').textContent, /2026\/10\/27受付/);
  state = queue();
  await h.tick(5000);
  assert.equal(h.el('called-panel').hidden, true);
  assert.equal(h.el('absent-panel').hidden, true);
});

test('zero waiting groups suppress a cached estimate and infer once when a positive queue returns', async () => {
  let state = queue([{ display_number: '20261028-001', total_people: 2, status: 'waiting' }]);
  const h = harness({ enter: false, queueFetch: async () => response(state, 200),
    predictFetch: async () => response(preview(), 200),
  });
  await settle();
  assert.equal(h.el('home-prediction-value').textContent, '約13分');
  assert.equal(h.predictRequests.length, 1);
  state = queue();
  await h.tick(5000);
  assert.match(h.el('home-prediction-value').textContent, /現在、参考待ち時間を表示できません/);
  assert.equal(h.predictRequests.length, 1);
  await h.tick(10000);
  assert.equal(h.predictRequests.length, 1);
  state = queue([{ display_number: '20261028-002', total_people: 3, status: 'waiting' }]);
  await h.tick(5000);
  assert.equal(h.predictRequests.length, 2);
  assert.equal(h.el('home-prediction-value').textContent, '約13分');
  await h.tick(10000);
  assert.equal(h.predictRequests.length, 2);
});

test('queue returning from zero does not request predictions for zero or blank party inputs', async () => {
  let state = queue();
  const h = harness({ queueFetch: async () => response(state, 200),
    predictFetch: async () => response(preview(), 200),
  });
  await h.input('adults', 0);
  await h.tick(400);
  const count = h.predictRequests.length;
  state = queue([{ display_number: '20261028-001', total_people: 2, status: 'waiting' }]);
  await h.tick(5000);
  assert.equal(h.predictRequests.length, count);
  assert.equal(h.el('submit-button').disabled, true);
  assert.doesNotMatch(h.el('entry-prediction-label').textContent, /NaN/);
  assert.doesNotMatch(h.el('entry-prediction-value').textContent, /NaN|約\d+分/);

  state = queue();
  await h.tick(5000);
  await h.input('adults', '');
  await h.tick(400);
  state = queue([{ display_number: '20261028-002', total_people: 3, status: 'waiting' }]);
  await h.tick(5000);
  assert.equal(h.predictRequests.length, count);
  assert.equal(h.el('submit-button').disabled, true);
  assert.doesNotMatch(h.el('entry-prediction-label').textContent, /NaN/);
  assert.doesNotMatch(h.el('entry-prediction-value').textContent, /NaN|約\d+分/);
  assert.equal(h.requests.length, 0);
  assert.equal(h.storage.getItem(STORAGE_KEY), null);
});

test('same-party periodic refresh retains its last reference with an updating label until the new result arrives', async () => {
  let finish;
  let attempts = 0;
  const h = harness({ enter: false, queueFetch: occupiedQueue,
    predictFetch: () => {
      attempts += 1;
      return attempts === 1 ? response(preview(), 200) : new Promise(resolve => { finish = resolve; });
    },
  });
  await settle();
  const originalRaw = h.el('home-prediction-raw').textContent;
  const originalWarnings = h.el('home-prediction-warnings').textContent;
  const originalWarningNode = h.el('home-prediction-warnings').children[0];
  assert.equal(h.el('home-prediction-value').textContent, '約13分');
  await h.tick(60000);
  assert.equal(h.predictRequests.length, 2);
  assert.equal(h.el('home-prediction-value').textContent, '約13分');
  assert.equal(h.el('home-prediction-raw').textContent, originalRaw);
  assert.equal(h.el('home-prediction-raw').hidden, false);
  assert.equal(h.el('home-prediction-warnings').textContent, originalWarnings);
  assert.equal(h.el('home-prediction-warnings').children[0], originalWarningNode);
  assert.match(h.el('home-prediction-updated').textContent, /更新中/);
  assert.equal(h.el('start-button').disabled, false);
  finish(response(preview({ minutes: 19.2 }), 200));
  await settle();
  assert.equal(h.el('home-prediction-value').textContent, '約20分');
  assert.match(h.el('home-prediction-raw').textContent, /19\.2/);
  assert.doesNotMatch(h.el('home-prediction-updated').textContent, /更新中/);
});

test('valid zero-minute preview from a positive queue remains a numeric reference with its raw value visible', async () => {
  const h = harness({ enter: false, queueFetch: occupiedQueue,
    predictFetch: async () => response(preview({ minutes: 0 }), 200),
  });
  await settle();
  assert.equal(h.el('home-prediction-value').textContent, '約0分');
  assert.match(h.el('home-prediction-raw').textContent, /0分/);
  assert.equal(h.el('home-prediction-raw').hidden, false);
  assert.equal(h.el('start-button').disabled, false);
  assert.equal(h.requests.length, 0);
});
