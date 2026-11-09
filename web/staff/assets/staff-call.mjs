// Synthetic call lab only. Business transitions and authorization stay on the server.
import { createCallJournal } from './staff-call-journal.mjs';

const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
  && value !== '00000000-0000-0000-0000-000000000000';
const integer = value => Number.isSafeInteger(value) && value >= 0;
const stamp = value => typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
const keys = (value, names) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...names].sort().join(',');
const RECEIPT_KEYS = ['operation_id', 'reception_id', 'action', 'outcome', 'response_status', 'recorded_at', 'resulting_revision', 'reception', 'error'];
const LABELS = { waiting: '待機中', called: '呼出中', absent: '不在', seated: '案内済み', cancelled: '取消済み' };

export const callLabResponse = response => response.headers?.get('X-Omachi-Preview') === 'synthetic'
  && response.headers?.get('X-Omachi-Preview-Actions') === 'call';
export const callLabMetadata = value => value?.schema_version === 2 && uuid(value.database_id) && uuid(value.write_epoch)
  && value.staff_write_mode === 'revision_required' && value.write_enabled === true && value.seat_corrections_enabled === false;

function validReception(value) {
  if (!keys(value, ['id', 'display_number', 'reception_order', 'received_at', 'adults', 'children', 'total_people',
    'status', 'called_at', 'seated_at', 'cancelled_at', 'actual_wait_minutes', 'snapshot', 'prediction'])) return false;
  const s = value.snapshot, p = value.prediction;
  return uuid(value.id) && typeof value.display_number === 'string' && value.display_number.length > 0
    && integer(value.reception_order) && value.reception_order > 0 && stamp(value.received_at)
    && integer(value.adults) && integer(value.children) && value.total_people === value.adults + value.children
    && value.total_people >= 1 && value.total_people <= 100 && value.status === 'called' && stamp(value.called_at)
    && Date.parse(value.called_at) >= Date.parse(value.received_at)
    && value.seated_at === null && value.cancelled_at === null && value.actual_wait_minutes === null
    && keys(s, ['captured_at', 'source', 'waiting_groups', 'ahead_groups', 'ahead_same_size_groups', 'party_size_groups'])
    && stamp(s.captured_at) && typeof s.source === 'string'
    && ['waiting_groups', 'ahead_groups', 'ahead_same_size_groups'].every(k => integer(s[k]))
    && keys(s.party_size_groups, ['1-2', '3-4', '5+']) && Object.values(s.party_size_groups).every(integer)
    && keys(p, ['status', 'minutes', 'model_version', 'predicted_at', 'warnings']) && stamp(p.predicted_at)
    && ['available', 'model_unavailable', 'no_waiting_groups', 'unsupported_weekday', 'unsupported_time', 'prediction_failed'].includes(p.status)
    && (p.status === 'available' ? typeof p.minutes === 'number' && Number.isFinite(p.minutes) && p.minutes >= 0
      && typeof p.model_version === 'string' && p.model_version.length > 0 : p.minutes === null)
    && (p.model_version === null || typeof p.model_version === 'string')
    && Array.isArray(p.warnings) && p.warnings.every(v => typeof v === 'string');
}

export function validCallReceipt(value, record) {
  if (!keys(value, RECEIPT_KEYS) || value.operation_id !== record.operation_id || value.reception_id !== record.reception_id
      || value.action !== 'call' || !stamp(value.recorded_at)
      || !(value.resulting_revision === null || integer(value.resulting_revision))) return false;
  if (value.outcome === 'rejected') {
    return value.reception === null && keys(value.error, ['code', 'detail'])
      && typeof value.error.detail === 'string' && value.error.detail.trim().length > 0
      && ['reception_not_found', 'revision_conflict', 'write_epoch_conflict', 'state_conflict'].includes(value.error.code)
      && value.response_status === (value.error.code === 'reception_not_found' ? 404 : 409);
  }
  if (!['state_changed', 'no_change'].includes(value.outcome) || value.response_status !== 200 || value.error !== null
      || !validReception(value.reception) || value.reception.id !== record.reception_id || !integer(value.resulting_revision)) return false;
  if (value.outcome === 'state_changed' && value.resulting_revision === 0) return false;
  return !record.request || value.resulting_revision === record.request.expected_revision + (value.outcome === 'state_changed' ? 1 : 0);
}

export function createStaffCall({ document: doc, window: env, fetchImpl, now, security, refresh, authFailure,
  validRow, validMetadata, rowSignature, metadataSignature }) {
  const get = id => doc.getElementById(id);
  const text = (id, value) => { if (get(id)?.textContent !== value) get(id).textContent = value; };
  const hide = (id, value) => { if (get(id)) get(id).hidden = value; };
  const disable = (id, value) => { if (get(id)) get(id).disabled = value; };
  let alive = true, snapshot = null, confirmation = null, confirmationVersion = 0, returnFocus = null;
  let busy = false, preparing = false, received = null, recoveredAuth = null;
  let phase = 'idle', notice = '', lockState = 'checking', releaseLock;
  // Presentation only: never read, resolve or remove a journal from this timer.
  let successTimer = null, successVersion = 0, successVisible = false, successHovered = false;
  let journal;
  try { journal = createCallJournal({ storage: env.sessionStorage, now }); }
  catch { journal = { read: () => ({ kind: 'blocked', reason: 'storage_unavailable' }) }; }

  const active = () => alive && security().active;
  const state = () => journal.read();
  const canStart = row => active() && security().ready && snapshot && lockState === 'owned' && !busy && !preparing
    && state().kind === 'empty' && integer(row.revision) && ['waiting', 'absent'].includes(row.status)
    && Array.isArray(row.allowed_actions) && row.allowed_actions.includes('call');

  function clearSuccess() {
    successVersion += 1; env.clearTimeout(successTimer); successTimer = null; successVisible = false; successHovered = false;
    hide('call-success-view', true); text('call-success-message', '');
    if (get('update-status')) get('update-status').dataset.callNotice = 'false';
  }

  function scheduleSuccess() {
    env.clearTimeout(successTimer); successTimer = null;
    const version = ++successVersion;
    if (!successVisible || successHovered || get('call-success-view')?.contains(doc.activeElement)) return;
    successTimer = env.setTimeout(() => {
      if (version === successVersion && successVisible && !successHovered && !get('call-success-view')?.contains(doc.activeElement)) clearSuccess();
    }, 5_000);
  }

  function showSuccess(message) {
    clearSuccess(); successVisible = true;
    text('call-success-message', message); hide('call-success-view', false);
    if (get('update-status')) get('update-status').dataset.callNotice = 'true';
    scheduleSuccess();
  }

  function render() {
    if (!active()) { hide('call-status-view', true); hide('call-confirmation', true); hide('call-backdrop', true); return; }
    const pending = state();
    const isPending = ['pending', 'manual'].includes(pending.kind);
    let message = notice;
    if (pending.kind === 'blocked') message = '復旧情報を安全に確認できません。新しい呼出を停止しています。管理者による確認が必要です。';
    else if (pending.kind === 'manual' && !busy) message = '復旧期限超過・要手動確認です。自動再送はしません。同じ操作IDの結果を照会してください。';
    else if (isPending && !message) message = '結果不明・要照会です。新しい呼出は開始できません。';
    else if (!message && lockState !== 'owned') message = '別タブで利用中、または安全なタブ間排他が利用できないため、このタブでは呼出できません。';
    hide('call-status-view', !message && !isPending);
    text('call-status-message', message);
    text('call-operation-id', isPending ? `操作ID：${pending.record.operation_id}` : '');
    hide('call-lookup', !isPending); disable('call-lookup', busy);
    hide('call-retry', pending.kind !== 'pending' || received !== null);
    disable('call-retry', busy || lockState !== 'owned' || !snapshot);
    disable('call-confirm', busy || preparing || !confirmation);
    hide('call-confirmation', !confirmation);
    hide('call-backdrop', !confirmation);
    if (get('call-status-view')) get('call-status-view').dataset.phase = phase;
  }

  function invalidate(message = '') {
    clearSuccess();
    confirmationVersion += 1; confirmation = null; preparing = false;
    returnFocus = null;
    hide('call-confirmation', true); get('call-confirm-fields')?.replaceChildren();
    if (message && state().kind === 'empty') { notice = message; phase = 'conflict'; }
    render();
  }

  function failAuth() { invalidate(); authFailure(); render(); }

  async function request(path, { method = 'GET', body } = {}) {
    if (!active()) throw new Error('inactive');
    const controller = new env.AbortController();
    let timer;
    try {
      const timeout = new Promise((_, reject) => {
        timer = env.setTimeout(() => { controller.abort(); reject(new Error('unknown')); }, 15_000);
      });
      const headers = { Accept: 'application/json' };
      if (method === 'POST') {
        if (!security().csrf) throw new Error('auth');
        headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = security().csrf;
      }
      const response = await Promise.race([fetchImpl(path, {
        method, headers, ...(body === undefined ? {} : { body }), signal: controller.signal,
        credentials: 'same-origin', mode: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
      }), timeout]);
      if (!callLabResponse(response)) throw new Error('environment');
      if ([401, 403].includes(response.status)) throw new Error('auth');
      const value = await Promise.race([response.json(), timeout]);
      return { status: response.status, value };
    } finally { env.clearTimeout(timer); }
  }

  async function context(id) {
    const response = await request(`/staff/receptions/${encodeURIComponent(id)}/operation-context`);
    const value = response.value;
    if (response.status !== 200 || !validMetadata(value?.metadata) || !callLabMetadata(value.metadata)
        || !validRow(value.reception) || value.reception.id !== id || !integer(value.reception.revision)
        || !Array.isArray(value.reception.allowed_actions)) throw new Error('context');
    return value;
  }

  function matches(a, b) {
    return metadataSignature(a.metadata) === metadataSignature(b.metadata) && rowSignature(a.reception) === rowSignature(b.reception);
  }

  async function prepare(row, restoreFocus) {
    if (!canStart(row)) return false;
    invalidate(); preparing = true; phase = 'confirming'; notice = '呼出対象の最新情報を確認しています。'; render();
    const version = confirmationVersion, auth = security().version;
    const original = { reception: row, metadata: snapshot.metadata };
    try {
      const latest = await context(row.id);
      if (!active() || version !== confirmationVersion || auth !== security().version) return false;
      const visible = snapshot?.rows.find(current => current.id === row.id);
      if (!matches(original, latest) || !visible || !matches(latest, { reception: visible, metadata: snapshot.metadata })
          || !['waiting', 'absent'].includes(latest.reception.status)
          || !latest.reception.allowed_actions.includes('call') || lockState !== 'owned') throw new Error('changed');
      confirmation = latest; returnFocus = restoreFocus; notice = '呼出確認中です。内容を確認して「呼出を確定」を押してください。';
      const fields = doc.createElement('dl'); fields.className = 'detail-fields';
      const add = (label, value) => {
        const dt = doc.createElement('dt'), dd = doc.createElement('dd');
        dt.textContent = label; dd.textContent = value; fields.append(dt, dd);
      };
      const r = latest.reception;
      add('受付番号（日付付き）', r.display_number);
      add('受付日時', new Date(r.received_at).toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' }));
      add('人数', `大人 ${r.adults}名 · 子供 ${r.children}名`); add('現在状態', LABELS[r.status]); add('実行する操作', '呼出');
      get('call-confirm-fields').replaceChildren(fields);
      hide('call-confirmation', false); get('call-dismiss')?.focus();
      return true;
    } catch (error) {
      if (active() && version === confirmationVersion && auth === security().version) {
        invalidate('受付情報を確認できないか、変更されました。最新の一覧から選び直してください。');
        if (error.message === 'auth') failAuth();
      }
      return false;
    } finally { if (version === confirmationVersion && auth === security().version) preparing = false; render(); }
  }

  function currentResponse(auth) { return active() && auth === security().version; }

  async function settle(receipt, record, auth) {
    received = receipt; // Memory only. A subsequent lookup repeats safe reads, never the POST.
    const current = await context(record.reception_id);
    if (!currentResponse(auth)) return false;
    if (current.metadata.database_id !== record.database_id
        || record.request && current.metadata.write_epoch !== record.request.write_epoch
        || receipt.resulting_revision !== null && current.reception.revision < receipt.resulting_revision) throw new Error('environment');
    if (!journal.resolve({ operation_id: record.operation_id, reception_id: record.reception_id, database_id: record.database_id })) throw new Error('storage');
    received = null;
    phase = receipt.outcome === 'rejected' ? 'rejected' : 'settling';
    notice = receipt.outcome === 'rejected' ? '呼出は拒否・競合しました。最新情報を確認し、必要なら改めて操作してください。'
      : receipt.outcome === 'no_change' ? '保存結果：変更なし。現在の受付状態は一覧で確認してください。'
        : '保存結果：呼出成功。現在の受付状態は再取得した一覧で確認してください。';
    // Never apply a historical receipt as today's reception row.
    const refreshed = await refresh();
    if (!currentResponse(auth)) return false;
    if (receipt.outcome !== 'rejected') {
      const latest = snapshot?.rows.find(row => row.id === record.reception_id);
      if (refreshed && snapshot && security().ready && latest && latest.revision >= current.reception.revision
          && metadataSignature(snapshot.metadata) === metadataSignature(current.metadata)) {
        phase = 'success'; notice = '';
        showSuccess(receipt.outcome === 'no_change' ? '保存結果：変更なし · 最新一覧を確認済み' : '保存結果：呼出成功 · 最新一覧を確認済み');
      } else {
        phase = 'review';
        notice = '操作結果は確定していますが、最新一覧を確認できません。呼出をやり直さず「最新情報を取得」で確認してください。';
      }
    }
    return true;
  }

  async function transmit(record) {
    const auth = security().version;
    phase = 'sending'; notice = '呼出送信中です。結果が確定するまで新しい呼出はできません。'; render();
    const fixed = journal.requestForRetry({ database_id: record.database_id, write_epoch: record.request.write_epoch });
    if (!fixed || JSON.stringify(fixed) !== JSON.stringify(record.request) || !snapshot || !security().ready || lockState !== 'owned') {
      phase = 'unknown'; notice = '固定要求・復旧期限・現在状態を確認できないため送信を停止しました。同じ操作IDの確認が必要です。'; render();
      return false;
    }
    try {
      const response = await request(`/staff/receptions/${record.reception_id}/operations`, { method: 'POST', body: JSON.stringify(record.request) });
      if (!currentResponse(auth)) return false;
      if (!validCallReceipt(response.value, record) || response.status !== response.value.response_status) throw new Error('unknown');
      return await settle(response.value, record, auth);
    } catch (error) {
      if (currentResponse(auth)) {
        phase = received?.outcome === 'rejected' ? 'rejected' : 'unknown';
        notice = received?.outcome === 'rejected' ? '保存結果：呼出拒否・競合。DB・世代を含む現在状態との照合が未完了のため、新規呼出は停止しています。'
          : received ? '操作結果は受領済みですが、現在状態を確認できません。同じ操作IDを照会してください。'
          : '結果不明・要照会です。通信エラーだけでは呼出の失敗とは判断できません。';
        if (error.message === 'auth') failAuth();
      }
      return false;
    }
  }

  async function confirm() {
    if (!confirmation || busy || preparing || !canStart(confirmation.reception)) { render(); return false; }
    busy = true; preparing = true; render();
    const selected = confirmation, version = confirmationVersion, auth = security().version;
    try {
      const latest = await context(selected.reception.id);
      if (!currentResponse(auth) || version !== confirmationVersion) return false;
      if (!matches(selected, latest) || lockState !== 'owned') throw new Error('changed');
      const operation_id = env.crypto.randomUUID();
      const record = journal.begin({ operation_id, reception_id: selected.reception.id, database_id: latest.metadata.database_id,
        request: { operation_id, action: 'call', expected_revision: latest.reception.revision, write_epoch: latest.metadata.write_epoch } });
      recoveredAuth = auth;
      invalidate();
      if (!record) { phase = 'blocked'; notice = '復旧情報を保存できないため、呼出は送信していません。'; return false; }
      return await transmit(record);
    } catch (error) {
      if (currentResponse(auth)) {
        invalidate('受付情報が変わったか、確定前の確認に失敗しました。選び直してください。');
        if (error.message === 'auth') failAuth();
      }
      return false;
    } finally { busy = false; preparing = false; render(); }
  }

  async function lookup() {
    const pending = state();
    if (!active() || busy || !['pending', 'manual'].includes(pending.kind)) return false;
    busy = true; phase = 'checking'; notice = '保存された操作結果と現在状態を照会しています。'; render();
    const auth = security().version, record = pending.record;
    try {
      // Establish the DB binding before asking for a historical receipt.
      const current = await context(record.reception_id);
      if (!currentResponse(auth)) return false;
      if (current.metadata.database_id !== record.database_id) throw new Error('environment');
      const response = await request(`/staff/operations/${record.operation_id}`);
      if (!currentResponse(auth)) return false;
      if (response.status !== 200 || !validCallReceipt(response.value, record)) throw new Error('unknown');
      return await settle(response.value, record, auth);
    } catch (error) {
      if (currentResponse(auth)) {
        phase = 'unknown'; notice = error.message === 'environment'
          ? '合成DBや書込み世代が変わっています。古い要求は送信せず、管理者の確認が必要です。'
          : '結果不明・要照会です。404や通信失敗は未実行の証明になりません。新規呼出は停止しています。';
        if (error.message === 'auth') failAuth();
      }
      return false;
    } finally { busy = false; render(); }
  }

  async function retry() {
    if (!active() || !security().ready || busy || received || lockState !== 'owned' || state().kind !== 'pending') return false;
    busy = true; render();
    const auth = security().version, version = confirmationVersion;
    try {
      const pending = state(), current = await context(pending.record.reception_id);
      if (!currentResponse(auth) || version !== confirmationVersion || !snapshot || !security().ready) return false;
      const record = journal.requestForRetry({ database_id: current.metadata.database_id, write_epoch: current.metadata.write_epoch });
      if (!record) throw new Error('environment');
      // The original revision/body are kept even if today's revision has advanced.
      return await transmit(pending.record);
    } catch (error) {
      if (currentResponse(auth)) {
        notice = '固定要求を安全に再送できません。新IDには変更せず、同じ操作IDの照会で確認してください。'; phase = 'unknown';
        if (error.message === 'auth') failAuth();
      }
      return false;
    } finally { busy = false; render(); }
  }

  function update(next) {
    snapshot = next;
    if (confirmation) {
      const row = next.rows.find(row => row.id === confirmation.reception.id);
      if (!row || !matches(confirmation, { reception: row, metadata: next.metadata })) invalidate('確認中の受付情報が変わりました。選び直してください。');
    }
    render();
    const pending = state(), version = security().version;
    if (pending.kind === 'pending' && recoveredAuth !== version && !busy) { recoveredAuth = version; void lookup(); }
  }

  function suspend() { snapshot = null; invalidate(); render(); }
  function dismiss() { const restore = returnFocus; invalidate(); if (active()) restore?.(); }
  function dismissSuccess() {
    const focused = doc.activeElement === get('call-success-close');
    clearSuccess();
    if (focused && active()) get('refresh-button')?.focus();
  }
  const successFocus = () => { successVersion += 1; env.clearTimeout(successTimer); successTimer = null; };
  const successBlur = () => { void Promise.resolve().then(scheduleSuccess); };
  const successEnter = () => { successHovered = true; successFocus(); };
  const successLeave = () => { successHovered = false; scheduleSuccess(); };
  const handlers = { 'call-confirm': () => { void confirm(); }, 'call-dismiss': dismiss,
    'call-lookup': () => { void lookup(); }, 'call-retry': () => { void retry(); }, 'call-success-close': dismissSuccess };
  for (const [id, handler] of Object.entries(handlers)) get(id)?.addEventListener('click', handler);
  const successHandlers = { focusin: successFocus, focusout: successBlur, pointerenter: successEnter, pointerleave: successLeave };
  for (const [type, handler] of Object.entries(successHandlers)) get('call-success-view')?.addEventListener(type, handler);
  const escape = event => {
    if (!confirmation) return;
    if (event.key === 'Escape') { event.preventDefault(); dismiss(); }
    if (event.key === 'Tab') {
      event.preventDefault();
      const dismissButton = get('call-dismiss'), confirmButton = get('call-confirm');
      const target = confirmButton.disabled ? dismissButton : doc.activeElement === dismissButton ? confirmButton
        : doc.activeElement === confirmButton ? dismissButton : event.shiftKey ? confirmButton : dismissButton;
      target?.focus();
    }
  };
  doc.addEventListener('keydown', escape);
  try {
    if (!env.navigator?.locks?.request) throw new Error('locks');
    void env.navigator.locks.request('omachi.synthetic.staff.call.v1', { mode: 'exclusive', ifAvailable: true }, async lock => {
      if (!alive) return;
      lockState = lock ? 'owned' : 'other'; render();
      if (lock) await new Promise(resolve => { releaseLock = resolve; });
    }).catch(() => { lockState = 'unavailable'; render(); });
  } catch { lockState = 'unavailable'; }

  return { prepare, confirm, lookup, retry, canStart, update, suspend, clearSuccess,
    stop() {
      alive = false; invalidate(); releaseLock?.();
      for (const [id, handler] of Object.entries(handlers)) get(id)?.removeEventListener('click', handler);
      for (const [type, handler] of Object.entries(successHandlers)) get('call-success-view')?.removeEventListener(type, handler);
      doc.removeEventListener('keydown', escape);
    },
  };
}
