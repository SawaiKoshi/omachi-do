// Presentation adapter for the verified 8445 synthetic lab only.
// Persistence, locks, requests and receipt validation belong to the foundation.
import { createStaffOperations } from './staff-operations.mjs';
import { canApplyOperation, operationLabMetadata } from './staff-operation-contract.mjs';

const LABELS = { waiting: '待機中', called: '呼出中', absent: '不在', seated: '案内済み', cancelled: '取消済み' };
const ACTIONS = { call: '呼出', absent: '不在' };

export function createStaffOperationUI({ document: doc, window: env, fetchImpl, now, security,
  refresh, authFailure, rowSignature, metadataSignature }) {
  const get = id => doc.getElementById(id);
  const text = (id, value) => { if (get(id) && get(id).textContent !== value) get(id).textContent = value; };
  const hide = (id, value) => { if (get(id)) get(id).hidden = value; };
  const disable = (id, value) => { if (get(id)) get(id).disabled = value; };
  let alive = true, snapshot = null, ticket = null, returnFocus = null, confirmationVersion = 0;
  let busy = false, preparing = false, message = '', phase = 'idle', recoveredVersion = null, settled = null;
  let submission = null;
  let successTimer = null, successVersion = 0, successVisible = false, hovered = false;
  const active = () => alive && security().active;
  const same = (a, b) => metadataSignature(a.metadata) === metadataSignature(b.metadata)
    && rowSignature(a.reception) === rowSignature(b.reception);
  function submissionCurrent() {
    if (!submission) return true;
    const row = snapshot?.rows.find(r => r.id === submission.reception.id);
    return Boolean(row && same(submission, { reception: row, metadata: snapshot.metadata }));
  }
  const engine = createStaffOperations({ window: env, fetchImpl, now, security: () => {
    const auth = security();
    // Keep the foundation's final pre-UUID readiness check false when a
    // newer background snapshot invalidated an in-flight confirmation.
    return { ...auth, ready: auth.ready && submissionCurrent() };
  }, authFailure: () => {
    closeDisplay();
    if (active()) authFailure();
  } });
  const current = version => active() && !engine.status().authExpired && security().version === version;

  function clearSuccess() {
    successVersion++; env.clearTimeout(successTimer); successTimer = null; successVisible = false; hovered = false;
    hide('call-success-view', true); text('call-success-message', '');
    if (get('update-status')) get('update-status').dataset.callNotice = 'false';
  }
  function scheduleSuccess() {
    env.clearTimeout(successTimer); successTimer = null;
    const version = ++successVersion;
    if (!successVisible || hovered || get('call-success-view')?.contains(doc.activeElement)) return;
    successTimer = env.setTimeout(() => {
      if (version === successVersion && successVisible && !hovered && !get('call-success-view')?.contains(doc.activeElement)) clearSuccess();
    }, 5_000);
  }
  function showSuccess(value) {
    clearSuccess(); successVisible = true;
    text('call-success-message', value); hide('call-success-view', false);
    if (get('update-status')) get('update-status').dataset.callNotice = 'true';
    scheduleSuccess();
  }
  function invalidate(value = '') {
    confirmationVersion++; ticket = null; returnFocus = null;
    get('call-confirm-fields')?.replaceChildren(); hide('call-confirmation', true); hide('call-backdrop', true);
    if (value) { message = value; phase = 'conflict'; }
  }
  function closeDisplay() {
    snapshot = null; submission = null; settled = null; message = ''; invalidate(); clearSuccess();
    text('call-status-message', ''); text('call-operation-id', '');
    hide('call-status-view', true); hide('call-lookup', true); hide('call-retry', true);
  }
  function canStart(row, action = 'call') {
    const state = engine.status();
    return Boolean(active() && security().ready && snapshot && operationLabMetadata(snapshot.metadata)
      && !busy && !preparing && !state.authExpired && !state.busy && state.lockState === 'owned'
      && state.journal.kind === 'empty' && canApplyOperation(action, row));
  }
  function render() {
    const state = engine.status();
    if (!active() || state.authExpired) { closeDisplay(); return; }
    const pending = ['pending', 'manual'].includes(state.journal.kind);
    let value = message;
    if (state.journal.kind === 'blocked') value = '復旧情報を安全に確認できません。呼出・不在を停止しています。管理者による確認が必要です。';
    else if (!busy && state.journal.kind === 'manual') value = '復旧期限超過・要手動確認です。再送はしません。同じ操作IDの結果を照会してください。';
    else if (pending && !value) value = '結果不明・要照会です。新しい呼出・不在は開始できません。';
    else if (!value && state.lockState !== 'owned') value = '別タブで利用中、または安全なタブ間排他が利用できないため、このタブでは操作できません。';
    text('call-status-message', value); hide('call-status-view', !value && !pending);
    text('call-operation-id', pending ? `操作ID：${state.journal.record.operation_id}` : '');
    hide('call-lookup', !pending); disable('call-lookup', busy || preparing);
    hide('call-retry', state.journal.kind !== 'pending' || state.journal.source !== 'v2' || state.receipt !== null);
    disable('call-retry', busy || preparing || !security().ready || state.lockState !== 'owned');
    disable('call-confirm', busy || preparing || !ticket?.context || !security().ready);
    hide('call-confirmation', !ticket); hide('call-backdrop', !ticket);
    if (get('call-status-view')) get('call-status-view').dataset.phase = phase;
  }
  async function prepare(row, restoreFocus, action = 'call') {
    if (!canStart(row, action)) return false;
    clearSuccess(); invalidate(); preparing = true; phase = 'confirming'; message = `${ACTIONS[action]}対象の最新情報を確認しています。`; render();
    const version = confirmationVersion, auth = security().version;
    const original = { reception: row, metadata: snapshot.metadata };
    try {
      const selected = await engine.prepare(action, row.id);
      if (!current(auth) || version !== confirmationVersion) return false;
      const latest = selected?.context, visible = snapshot?.rows.find(r => r.id === row.id);
      if (!latest || !visible || !same(original, latest)
          || !same(latest, { reception: visible, metadata: snapshot.metadata })) {
        invalidate('受付情報が変わったか確認できません。最新の一覧から選び直してください。'); return false;
      }
      ticket = selected; returnFocus = restoreFocus;
      const fields = doc.createElement('dl'); fields.className = 'detail-fields';
      const add = (label, value) => {
        const dt = doc.createElement('dt'), dd = doc.createElement('dd');
        dt.textContent = label; dd.textContent = value; fields.append(dt, dd);
      };
      const r = latest.reception;
      add('受付番号（日付付き）', r.display_number);
      add('受付日時', new Date(r.received_at).toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' }));
      add('人数', `大人 ${r.adults}名 · 子供 ${r.children}名`); add('現在状態', LABELS[r.status]); add('実行する操作', ACTIONS[action]);
      get('call-confirm-fields').replaceChildren(fields);
      text('call-confirm-title', `${ACTIONS[action]}の確認（合成データ限定）`); text('call-confirm', `${ACTIONS[action]}を確定`);
      message = `${ACTIONS[action]}確認中です。内容を確認して確定してください。`;
      hide('call-confirmation', false); get('call-dismiss')?.focus();
      return true;
    } finally { preparing = false; render(); }
  }
  function verifiedList(value) {
    if (!snapshot || !security().ready || metadataSignature(snapshot.metadata) !== metadataSignature(value.current.metadata)) return false;
    const row = snapshot.rows.find(r => r.id === value.current.reception.id), expected = value.current.reception;
    return row && row.revision >= expected.revision && (row.revision > expected.revision || rowSignature(row) === rowSignature(expected));
  }
  function finishList() {
    if (!settled || !verifiedList(settled)) return false;
    const receipt = settled.receipt;
    settled = null;
    if (receipt.outcome !== 'rejected') {
      message = ''; phase = 'success';
      showSuccess(receipt.outcome === 'no_change' ? '保存結果：変更なし · 最新一覧を確認済み'
        : `保存結果：${ACTIONS[receipt.action]}成功 · 最新一覧を確認済み`);
    }
    return true;
  }
  async function run(method) {
    const before = engine.status();
    if (!active() || before.authExpired || busy || preparing || method === 'confirm' && !ticket) return false;
    const selected = ticket, auth = security().version;
    submission = method === 'confirm' ? selected.context : null;
    recoveredVersion = auth;
    busy = true; clearSuccess(); settled = null;
    phase = method === 'lookup' ? 'checking' : 'sending';
    message = method === 'lookup' ? '保存された操作結果と現在状態を照会しています。'
      : '最新状態を確認・送信中です。結果が確定するまで新しい操作はできません。';
    invalidate(); render();
    try {
      const result = await engine[method](...(method === 'confirm' ? [selected] : []));
      if (!current(auth)) return false;
      const state = engine.status();
      if (!result) {
        const pending = ['pending', 'manual', 'blocked'].includes(state.journal.kind);
        phase = pending ? 'unknown' : 'conflict';
        message = pending ? state.receipt ? '操作結果は受領済みですが、現在状態との照合が未完了です。同じ操作IDを照会してください。'
          : '結果不明・要照会です。通信失敗・404・タイムアウトは未実行の証明になりません。新しい操作は停止しています。'
          : '確認情報が変わったか、安全に確認できませんでした。最新の一覧から選び直してください。';
        return false;
      }
      settled = { receipt: state.receipt, current: state.current };
      phase = state.receipt.outcome === 'rejected' ? 'rejected' : 'review';
      message = state.receipt.outcome === 'rejected' ? '保存結果：操作は拒否・競合しました。最新状態を確認し、必要なら改めて選び直してください。'
        : '操作結果は確定していますが、最新一覧は未確認です。操作をやり直さず「最新情報を取得」で確認してください。';
      const refreshed = await refresh();
      if (!current(auth)) return false;
      if (refreshed) finishList();
      return true;
    } finally { submission = null; busy = false; render(); }
  }
  function update(next) {
    snapshot = next;
    if (ticket) {
      const context = ticket.context, row = context && next.rows.find(r => r.id === context.reception.id);
      if (!row || !same(context, { reception: row, metadata: next.metadata })) invalidate('確認中の受付情報が変わりました。選び直してください。');
    }
    if (!busy && settled) finishList();
    render();
    const state = engine.status(), version = security().version;
    // Reinitialization never POSTs. Only the stored ID is queried once after
    // an authenticated all-page snapshot; an explicit button can query again.
    if (active() && !state.authExpired && ['pending', 'manual'].includes(state.journal.kind)
        && recoveredVersion !== version && !busy && !preparing) { recoveredVersion = version; void run('lookup'); }
  }
  function suspend() {
    snapshot = null; invalidate(); clearSuccess();
    if (settled && settled.receipt.outcome !== 'rejected') { phase = 'review'; message = '操作結果は確定していますが、最新一覧は未確認です。再送せず最新情報を取得してください。'; }
    render();
  }
  function dismiss() {
    const restore = returnFocus; invalidate(); message = ''; phase = 'idle'; render(); if (active()) restore?.();
  }
  function dismissSuccess() {
    const focused = doc.activeElement === get('call-success-close'); clearSuccess();
    if (focused && active()) get('refresh-button')?.focus();
  }
  const handlers = { 'call-confirm': () => { void run('confirm'); }, 'call-dismiss': dismiss,
    'call-lookup': () => { void run('lookup'); }, 'call-retry': () => { void run('retry'); }, 'call-success-close': dismissSuccess };
  const pauseSuccess = () => { successVersion++; env.clearTimeout(successTimer); successTimer = null; };
  const successHandlers = { focusin: pauseSuccess, focusout: () => { void Promise.resolve().then(scheduleSuccess); },
    pointerenter: () => { hovered = true; pauseSuccess(); }, pointerleave: () => { hovered = false; scheduleSuccess(); } };
  const keyDown = event => {
    if (!ticket) return;
    if (event.key === 'Escape') { event.preventDefault(); dismiss(); }
    if (event.key === 'Tab') {
      event.preventDefault(); const cancel = get('call-dismiss'), confirm = get('call-confirm');
      (confirm.disabled ? cancel : doc.activeElement === cancel ? confirm : doc.activeElement === confirm ? cancel : event.shiftKey ? confirm : cancel)?.focus();
    }
  };
  for (const [id, handler] of Object.entries(handlers)) get(id)?.addEventListener('click', handler);
  for (const [type, handler] of Object.entries(successHandlers)) get('call-success-view')?.addEventListener(type, handler);
  doc.addEventListener('keydown', keyDown);
  return { canStart, prepare, update, suspend, clearSuccess,
    isConfirming: () => preparing || ticket !== null,
    expireAuthentication() { engine.expireAuthentication(); closeDisplay(); },
    stop() {
      alive = false; engine.stop(); closeDisplay();
      for (const [id, handler] of Object.entries(handlers)) get(id)?.removeEventListener('click', handler);
      for (const [type, handler] of Object.entries(successHandlers)) get('call-success-view')?.removeEventListener(type, handler);
      doc.removeEventListener('keydown', keyDown);
    },
  };
}
