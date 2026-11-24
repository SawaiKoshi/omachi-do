// Presentation adapter for the verified 8445 and 8446 synthetic labs.
// Persistence, locks, requests and receipt validation belong to the foundation.
import { createStaffOperations } from './staff-operations.mjs';
import { canApplyOperation, operationLabMetadata } from './staff-operation-contract.mjs';

const ACTIONS = { call: '呼出', absent: '不在', seat: '案内完了' };

export function createStaffOperationUI({ document: doc, window: env, fetchImpl, now, seatOnly = false, security,
  refresh, authFailure, rowSignature, metadataSignature }) {
  const get = id => doc.getElementById(id);
  const text = (id, value) => { if (get(id) && get(id).textContent !== value) get(id).textContent = value; };
  const hide = (id, value) => { if (get(id)) get(id).hidden = value; };
  const disable = (id, value) => { if (get(id)) get(id).disabled = value; };
  let alive = true, snapshot = null, operationVersion = 0;
  let busy = false, message = '', phase = 'idle', recoveredVersion = null, settled = null;
  let submission = null;
  let successTimer = null, successVersion = 0, successVisible = false, hovered = false;
  const active = () => alive && security().active;
  const same = (a, b) => metadataSignature(a.metadata) === metadataSignature(b.metadata)
    && rowSignature(a.reception) === rowSignature(b.reception);
  function submissionCurrent() {
    if (!submission) return true;
    if (submission.version !== operationVersion) return false;
    const row = snapshot?.rows.find(r => r.id === submission.context.reception.id);
    return Boolean(row && same(submission.context, { reception: row, metadata: snapshot.metadata }));
  }
  const engine = createStaffOperations({ window: env, fetchImpl, now, seatOnly, security: () => {
    const auth = security();
    // Keep the foundation's final pre-UUID readiness check false when a
    // newer background snapshot invalidated the selected operation.
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
    operationVersion++;
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
      && !busy && !state.authExpired && !state.busy && state.lockState === 'owned'
      && state.journal.kind === 'empty' && canApplyOperation(action, row));
  }
  function render() {
    const state = engine.status();
    if (!active() || state.authExpired) { closeDisplay(); return; }
    const pending = ['pending', 'manual'].includes(state.journal.kind);
    let value = message;
    if (state.journal.kind === 'blocked') value = '復旧情報を安全に確認できません。操作を停止しています。管理者による確認が必要です。';
    else if (!busy && state.journal.kind === 'manual') value = '復旧期限超過・要手動確認です。再送はしません。同じ操作IDの結果を照会してください。';
    else if (pending && !value) value = '結果不明・要照会です。新しい操作は開始できません。';
    else if (!value && state.lockState !== 'owned') value = '別タブで利用中、または安全なタブ間排他が利用できないため、このタブでは操作できません。';
    text('call-status-message', value); hide('call-status-view', !value && !pending);
    text('call-operation-id', pending ? `操作ID：${state.journal.record.operation_id}` : '');
    hide('call-lookup', !pending); disable('call-lookup', busy);
    hide('call-retry', state.journal.kind !== 'pending' || state.journal.source !== 'v2' || state.receipt !== null);
    disable('call-retry', busy || !security().ready || state.lockState !== 'owned');
    if (get('call-status-view')) get('call-status-view').dataset.phase = phase;
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
  async function run(method, row, action) {
    const before = engine.status();
    if (!active() || before.authExpired || busy || method === 'execute' && !canStart(row, action)) { render(); return false; }
    const auth = security().version;
    const original = method === 'execute' ? { reception: row, metadata: snapshot.metadata } : null;
    recoveredVersion = auth;
    busy = true; clearSuccess(); settled = null;
    phase = method === 'lookup' ? 'checking' : 'sending';
    message = method === 'lookup' ? '保存された操作結果と現在状態を照会しています。'
      : method === 'execute' ? `${row.display_number}の${ACTIONS[action]}を処理中です。結果が確定するまで新しい操作はできません。`
        : '固定要求を送信中です。結果が確定するまで新しい操作はできません。';
    invalidate();
    submission = original ? { context: original, version: operationVersion } : null;
    const version = operationVersion;
    render();
    try {
      let result;
      if (method === 'execute') {
        const selected = await engine.prepare(action, row.id);
        const latest = selected?.context;
        if (!current(auth)) return false;
        if (version !== operationVersion) {
          phase = 'conflict'; message = '操作条件が変わりました。最新の一覧から選び直してください。';
          return false;
        }
        if (!latest || !same(original, latest) || !submissionCurrent()) {
          phase = 'conflict'; message = '受付情報が変わったか確認できません。最新の一覧から選び直してください。';
          return false;
        }
        result = await engine.confirm(selected);
      } else result = await engine[method]();
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
    if (!busy && settled) finishList();
    render();
    const state = engine.status(), version = security().version;
    // Reinitialization never POSTs. Only the stored ID is queried once after
    // an authenticated all-page snapshot; an explicit button can query again.
    if (active() && !state.authExpired && ['pending', 'manual'].includes(state.journal.kind)
        && recoveredVersion !== version && !busy) { recoveredVersion = version; void run('lookup'); }
  }
  function suspend() {
    snapshot = null; invalidate(); clearSuccess();
    if (settled && settled.receipt.outcome !== 'rejected') { phase = 'review'; message = '操作結果は確定していますが、最新一覧は未確認です。再送せず最新情報を取得してください。'; }
    render();
  }
  function dismissSuccess() {
    const focused = doc.activeElement === get('call-success-close'); clearSuccess();
    if (focused && active()) get('refresh-button')?.focus();
  }
  const handlers = { 'call-lookup': () => { void run('lookup'); }, 'call-retry': () => { void run('retry'); }, 'call-success-close': dismissSuccess };
  const pauseSuccess = () => { successVersion++; env.clearTimeout(successTimer); successTimer = null; };
  const successHandlers = { focusin: pauseSuccess, focusout: () => { void Promise.resolve().then(scheduleSuccess); },
    pointerenter: () => { hovered = true; pauseSuccess(); }, pointerleave: () => { hovered = false; scheduleSuccess(); } };
  for (const [id, handler] of Object.entries(handlers)) get(id)?.addEventListener('click', handler);
  for (const [type, handler] of Object.entries(successHandlers)) get('call-success-view')?.addEventListener(type, handler);
  return { canStart, execute: (row, action) => run('execute', row, action), update, suspend, clearSuccess,
    expireAuthentication() { engine.expireAuthentication(); closeDisplay(); },
    stop() {
      alive = false; engine.stop(); closeDisplay();
      for (const [id, handler] of Object.entries(handlers)) get(id)?.removeEventListener('click', handler);
      for (const [type, handler] of Object.entries(successHandlers)) get('call-success-view')?.removeEventListener(type, handler);
    },
  };
}
