// Staff read-only UI. Credentials and CSRF live only in request-local memory.
const POLL_INTERVAL = 5_000;
const REQUEST_TIMEOUT = 15_000;
const PAGE_LIMIT = 100;
const MAX_PAGES = 100;
const ACTION_LABELS = { call: "呼出", absent: "不在", return: "復帰", seat: "案内完了", cancel: "受付取消", seating_correction: "誤案内訂正" };
const LABELS = { waiting: "待機中", called: "呼出中", absent: "不在", seated: "案内済み", cancelled: "取消済み" };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const integer = (value, min = 0) => Number.isSafeInteger(value) && value >= min;
const timestamp = value => typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));

class ReadError extends Error {
  constructor(code, status = null) { super(code); this.code = code; this.status = status; }
}

function validSession(value) {
  return value?.authenticated === true && typeof value.csrf_token === "string" && value.csrf_token.length > 0
    && integer(value.expires_in_seconds, 1) && value.expires_in_seconds <= 3600
    && integer(value.idle_timeout_seconds, 1) && value.idle_timeout_seconds <= 900;
}

function validMetadata(value) {
  return value && [1, 2].includes(value.schema_version) && typeof value.view_version === "string" && value.view_version.length > 0
    && ["legacy", "revision_required"].includes(value.staff_write_mode)
    && typeof value.write_enabled === "boolean" && typeof value.seat_corrections_enabled === "boolean"
    && (value.schema_version === 1 ? value.database_id === null && value.write_epoch === null
      : typeof value.database_id === "string" && typeof value.write_epoch === "string");
}

function validRow(row) {
  const event = row?.valid_seating_event;
  return row && typeof row.id === "string" && UUID.test(row.id) && typeof row.display_number === "string"
    && row.display_number.length > 0 && integer(row.reception_order, 1) && timestamp(row.received_at)
    && integer(row.adults) && integer(row.children) && integer(row.total_people, 1)
    && row.total_people === row.adults + row.children && row.total_people <= 100
    && Object.hasOwn(LABELS, row.status) && (row.revision === null || integer(row.revision))
    && (row.actual_wait_minutes === null || typeof row.actual_wait_minutes === "number"
      && Number.isFinite(row.actual_wait_minutes) && row.actual_wait_minutes >= 0)
    && (event === null || event && integer(event.event_id, 1)
      && ["waiting", "called", "absent"].includes(event.from_status) && event.to_status === "seated"
      && timestamp(event.occurred_at) && typeof event.correctable === "boolean");
}

// Compare the displayed contract, independently of JSON property order. The
// global view version may advance for a different reception without changing
// this reception's read-only interaction target.
function rowSignature(row) {
  const seat = row.valid_seating_event;
  return JSON.stringify([
    ...["id", "display_number", "reception_order", "received_at", "adults", "children", "total_people",
      "status", "called_at", "seated_at", "cancelled_at", "actual_wait_minutes", "revision"].map(key => row[key]),
    Array.isArray(row.allowed_actions) ? [...new Set(row.allowed_actions.filter(action => typeof action === "string"))].sort() : null,
    seat === null ? null : [seat.event_id, seat.from_status, seat.to_status, seat.occurred_at, seat.correctable],
  ]);
}

function metadataSignature(value) {
  return JSON.stringify([value.schema_version, value.database_id, value.write_epoch, value.staff_write_mode,
    value.seat_corrections_enabled, value.write_enabled, value.disabled_reason]);
}

function numberText(value) {
  const match = /^(\d{4})(\d{2})(\d{2})-(\d+)$/.exec(value);
  return match ? `${match[1]}/${match[2]}/${match[3]} · ${match[4]}番` : value;
}

function timeText(value) {
  return timestamp(value) ? new Date(value).toLocaleString("ja-JP", {
    timeZone: "Asia/Tokyo", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }) : "—";
}

export function createStaffApp({ document: doc, fetchImpl, window: env = globalThis, now = () => Date.now() }) {
  const get = id => doc.getElementById(id);
  const page = doc.body.dataset.staffPage;
  const fetcher = fetchImpl ?? env.fetch.bind(env);
  const secureOrigin = () => env.location.protocol === "https:" && env.location.hostname === "staff.localhost";
  let started = false, paused = false, needsLogin = false, authenticated = false;
  let loginBusy = false, logoutBusy = false, logoutPending = false;
  let csrf = "", logoutCsrf = "", absoluteDeadline = null;
  let authVersion = 0, refreshVersion = 0, detailVersion = 0;
  let pollTimer = null, expiryTimer = null;
  let displayedVersion = null, displayedMetadata = null, detailTarget = null;
  let selectedTab = "active", menuEntry = null, gesture = null;
  let detailReturnTarget = null;
  let suppressClickUntil = 0;
  const pointers = new Set(), rowNodes = new Map(), scrollPositions = { active: 0, recently_seated: 0 };
  const controllers = new Map();
  const text = (id, value) => { if (get(id) && get(id).textContent !== value) get(id).textContent = value; };
  const hidden = (id, value) => { if (get(id) && get(id).hidden !== value) get(id).hidden = value; };
  const disabled = (id, value) => { if (get(id) && get(id).disabled !== value) get(id).disabled = value; };
  const message = (value, login = false) => {
    const id = login ? "login-message" : "status-message";
    text(id, value); hidden(id, !value);
    if (!login) hidden("updated-at", Boolean(value));
  };

  function canBrowse() {
    return started && authenticated && !paused && !needsLogin && !logoutPending
      && displayedVersion !== null && !get("staff-view")?.hidden && get("staff-view")?.dataset.state === "current";
  }

  function closeMenu({ restoreFocus = false } = {}) {
    const previous = menuEntry;
    menuEntry = null; gesture = null;
    if (previous) {
      previous.trigger.setAttribute("aria-expanded", "false");
      previous.item.dataset.menuOpen = "false";
    }
    hidden("row-menu", true);
    text("row-menu-title", "受付メニュー"); text("row-menu-status", "");
    get("row-menu-items")?.replaceChildren();
    if (restoreFocus && previous && canBrowse() && rowNodes.get(previous.key) === previous) previous.trigger.focus();
  }

  function applyTabs() {
    for (const [view, prefix] of [["active", "active"], ["recently_seated", "recent"]]) {
      get(`${prefix}-tab`)?.setAttribute("aria-selected", String(selectedTab === view));
      get(`${prefix}-tab`)?.setAttribute("tabindex", selectedTab === view ? "0" : "-1");
      hidden(`${prefix}-panel`, selectedTab !== view);
    }
  }

  function selectTab(view, { focus = false } = {}) {
    if (!canBrowse() || !["active", "recently_seated"].includes(view)) return false;
    closeMenu(); closeDetail();
    const scroll = get("reception-scroll");
    if (scroll) scrollPositions[selectedTab] = scroll.scrollTop;
    selectedTab = view; applyTabs();
    if (scroll) scroll.scrollTop = scrollPositions[view];
    if (focus) get(view === "active" ? "active-tab" : "recent-tab")?.focus();
    return true;
  }

  function openMenu(entry) {
    if (!canBrowse() || entry.view !== selectedTab || rowNodes.get(entry.key) !== entry) return false;
    closeMenu(); closeDetail();
    menuEntry = entry;
    entry.trigger.setAttribute("aria-expanded", "true"); entry.item.dataset.menuOpen = "true";
    text("row-menu-title", `${numberText(entry.row.display_number)}のメニュー`);
    text("row-menu-status", `現在状態：${LABELS[entry.row.status]}`);
    const actions = Array.isArray(entry.row.allowed_actions)
      ? [...new Set(entry.row.allowed_actions)].filter(action => typeof action === "string" && Object.hasOwn(ACTION_LABELS, action)) : [];
    get("row-menu-items").replaceChildren(...(actions.length ? actions.map(action => element("li", `${ACTION_LABELS[action]}（未接続・実行不可）`))
      : [element("li", "この受付に利用可能な操作項目はありません。") ]));
    hidden("row-menu", false); get("row-menu").focus();
    return true;
  }

  function trackMove(event) {
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
    gesture.dx = dx;
    gesture.maxY = Math.max(gesture.maxY, Math.abs(dy));
    gesture.moved = gesture.moved || Math.hypot(dx, dy) >= 12;
    if (dx < -12 || gesture.maxY > 20 || Math.abs(dy) >= 12 && Math.abs(dy) > Math.abs(dx) * .6) gesture.cancelled = true;
  }

  function bindRowGestures(entry) {
    entry.item.addEventListener("pointerdown", event => {
      if (!canBrowse() || entry.view !== selectedTab || !["touch", "pen"].includes(event.pointerType)
          || event.isPrimary === false || event.button !== 0 || !Number.isInteger(event.pointerId)
          || pointers.size > 1 || entry.trigger.contains(event.target)) return;
      gesture = { entry, pointerId: event.pointerId, x: event.clientX, y: event.clientY,
        startedAt: now(), dx: 0, maxY: 0, moved: false, cancelled: false };
    });
    entry.item.addEventListener("pointermove", trackMove);
    entry.item.addEventListener("pointerup", event => {
      if (!gesture || gesture.entry !== entry || gesture.pointerId !== event.pointerId) return;
      trackMove(event);
      const completed = gesture; gesture = null;
      if (completed.moved) suppressClickUntil = now() + 500;
      if (!completed.cancelled && completed.dx >= 72 && completed.dx >= completed.maxY * 3
          && now() - completed.startedAt <= 1200 && canBrowse()) openMenu(entry);
    });
    for (const type of ["pointercancel", "lostpointercapture", "pointerleave"]) {
      entry.item.addEventListener(type, () => {
        if (gesture?.entry === entry) { if (gesture.moved) suppressClickUntil = now() + 500; gesture = null; }
      });
    }
  }

  function closeDetail({ restoreFocus = false } = {}) {
    const target = detailReturnTarget; detailReturnTarget = null;
    detailTarget = null;
    detailVersion += 1;
    hidden("detail-view", true);
    text("detail-title", "受付詳細");
    get("detail-content")?.replaceChildren();
    if (restoreFocus && target && canBrowse() && [...rowNodes.values()].some(entry => entry.button === target)) target.focus();
  }

  function clearData() {
    closeMenu(); pointers.clear(); rowNodes.clear(); selectedTab = "active"; applyTabs();
    scrollPositions.active = 0; scrollPositions.recently_seated = 0;
    if (get("reception-scroll")) get("reception-scroll").scrollTop = 0;
    disabled("active-tab", true); disabled("recent-tab", true);
    displayedVersion = null; displayedMetadata = null;
    for (const id of ["active-list", "recent-list"]) get(id)?.replaceChildren();
    for (const id of ["waiting-count", "called-count", "absent-count"]) text(id, "—");
    for (const id of ["active-empty", "recent-empty"]) hidden(id, true);
    text("updated-at", "最新情報は取得できていません");
    closeDetail();
  }

  function abortReads({ preserveInteraction = false } = {}) {
    if (!preserveInteraction) { closeMenu(); pointers.clear(); }
    refreshVersion += 1;
    if (!preserveInteraction) detailVersion += 1;
    for (const [controller, purpose] of controllers) {
      if (!preserveInteraction || purpose !== "detail") controller.abort();
    }
    env.clearTimeout(pollTimer); pollTimer = null;
  }

  function lockData(value, { expired = false } = {}) {
    authVersion += 1;
    authenticated = false; csrf = "";
    abortReads(); clearData(); hidden("staff-view", true);
    disabled("logout-button", !logoutPending);
    disabled("refresh-button", expired || logoutPending);
    if (expired) needsLogin = true;
    message(value, page === "login");
  }

  async function request(path, { method = "GET", headers = {}, purpose = "refresh" } = {}) {
    if (!secureOrigin()) throw new ReadError("insecure");
    const controller = new env.AbortController();
    controllers.set(controller, purpose);
    let timeout, abortHandler;
    try {
      const interrupted = new Promise((_, reject) => {
        abortHandler = () => reject(new ReadError("cancelled"));
        controller.signal.addEventListener("abort", abortHandler, { once: true });
        timeout = env.setTimeout(() => { reject(new ReadError("network")); controller.abort(); }, REQUEST_TIMEOUT);
      });
      const response = await Promise.race([fetcher(path, {
        method, credentials: "same-origin", mode: "same-origin", cache: "no-store", redirect: "error",
        referrerPolicy: "no-referrer", signal: controller.signal, headers: { Accept: "application/json", ...headers },
      }), interrupted]);
      hidden("synthetic-preview", response.headers?.get("X-Omachi-Preview") !== "synthetic");
      if ([401, 403].includes(response.status)) throw new ReadError("auth", response.status);
      if (response.status === 409) throw new ReadError("conflict");
      if (response.status === 429) throw new ReadError("rate");
      if (!response.ok) throw new ReadError("network");
      // Keep the timeout active while receiving/parsing the response body too.
      return await Promise.race([response.json(), interrupted]);
    } catch (error) {
      throw error instanceof ReadError ? error : new ReadError("network");
    } finally {
      env.clearTimeout(timeout);
      controller.signal.removeEventListener("abort", abortHandler);
      controllers.delete(controller);
    }
  }

  function acceptSession(value, requestStarted) {
    if (!validSession(value)) throw new ReadError("auth");
    csrf = value.csrf_token; authenticated = true; needsLogin = false;
    absoluteDeadline = Math.min(absoluteDeadline ?? Infinity, requestStarted + value.expires_in_seconds * 1000);
    const expires = Math.min(absoluteDeadline, requestStarted + value.idle_timeout_seconds * 1000);
    if (expires <= now()) throw new ReadError("auth");
    env.clearTimeout(expiryTimer);
    expiryTimer = env.setTimeout(() => {
      lockData("セッションの期限が切れました。再ログインしてください。", { expired: true });
      env.location.replace("/staff/login");
    }, expires - now());
    disabled("logout-button", false);
  }

  function schedulePoll() {
    env.clearTimeout(pollTimer);
    if (started && !paused && !needsLogin && !logoutPending && page === "receptions") {
      pollTimer = env.setTimeout(() => { void refresh(); }, POLL_INTERVAL);
    }
  }

  async function allPages(view, current) {
    let cursor = null, version = null, asOf = null, previousKey = null, metadata = null;
    const rows = [], cursors = new Set(), ids = new Set();
    for (let index = 0; index < MAX_PAGES; index += 1) {
      const params = new URLSearchParams({ view, limit: String(PAGE_LIMIT), hours: "24" });
      if (cursor !== null) params.set("cursor", cursor);
      const pageResult = await request("/staff/receptions?" + params.toString());
      if (!current()) throw new ReadError("cancelled");
      if (!validMetadata(pageResult?.metadata) || pageResult.view !== view || !timestamp(pageResult.as_of)
          || !Array.isArray(pageResult.receptions) || pageResult.receptions.length > PAGE_LIMIT
          || typeof pageResult.has_more !== "boolean") throw new ReadError("invalid");
      const pageVersion = pageResult.metadata.view_version;
      version = version ?? pageVersion; asOf = asOf ?? pageResult.as_of;
      metadata = metadata ?? pageResult.metadata;
      if (version !== pageVersion || asOf !== pageResult.as_of
          || metadataSignature(metadata) !== metadataSignature(pageResult.metadata)) throw new ReadError("conflict");
      for (const row of pageResult.receptions) {
        if (!validRow(row) || ids.has(row.id)) throw new ReadError("invalid");
        const active = ["waiting", "called", "absent"].includes(row.status);
        if (view === "active" ? !active : row.status !== "seated" || row.valid_seating_event === null) throw new ReadError("invalid");
        const key = view === "active" ? row.reception_order : row.valid_seating_event.event_id;
        if (previousKey !== null && (view === "active" ? key <= previousKey : key >= previousKey)) throw new ReadError("invalid");
        previousKey = key; ids.add(row.id); rows.push(row);
      }
      if (!pageResult.has_more) {
        if (pageResult.next_cursor !== null) throw new ReadError("invalid");
        return { rows, version, metadata };
      }
      cursor = pageResult.next_cursor;
      if (pageResult.receptions.length === 0 || typeof cursor !== "string" || !cursor.length
          || cursor.length > 2048 || cursors.has(cursor)) throw new ReadError("invalid");
      cursors.add(cursor);
    }
    throw new ReadError("incomplete");
  }

  function element(tag, value, className) {
    const result = doc.createElement(tag);
    if (value !== undefined) result.textContent = value;
    if (className) result.className = className;
    return result;
  }

  function renderRows(id, rows) {
    const view = id === "active-list" ? "active" : "recently_seated";
    const list = get(id), retained = new Set(rows.map(row => `${view}:${row.id}`));
    for (const [key, entry] of rowNodes) {
      if (entry.view === view && !retained.has(key)) {
        list.removeChild(entry.item); rowNodes.delete(key);
      }
    }
    rows.forEach((row, index) => {
      const key = `${view}:${row.id}`, signature = rowSignature(row), previous = rowNodes.get(key);
      if (previous?.signature === signature) {
        if (list.children[index] !== previous.item) list.insertBefore(previous.item, list.children[index] ?? null);
        return;
      }
      const item = element("li", undefined, "reception-row");
      item.dataset.receptionId = row.id;
      const button = element("button", undefined, "ticket");
      const parts = /^(\d{4})(\d{2})(\d{2})-(\d+)$/.exec(row.display_number);
      if (parts) button.append(element("span", `${parts[4]}番`, "ticket-main"), element("span", `${parts[1]}/${parts[2]}/${parts[3]}`, "ticket-date"));
      else button.textContent = row.display_number;
      button.type = "button";
      button.setAttribute("aria-label", `${numberText(row.display_number)}の詳細を確認`);
      button.addEventListener("click", () => { void openDetail(row.id); });
      const time = element("span", undefined, "row-time");
      if (view === "recently_seated") time.append(element("span", `案内 ${timeText(row.valid_seating_event.occurred_at)}`));
      time.append(element("span", `受付 ${timeText(row.received_at)}`));
      const trigger = element("button", "⋯", "menu-toggle");
      trigger.type = "button";
      trigger.setAttribute("aria-label", `${numberText(row.display_number)}の閲覧専用メニュー`);
      trigger.setAttribute("aria-expanded", "false"); trigger.setAttribute("aria-controls", "row-menu");
      const entry = { key, view, row, signature, item, button, trigger };
      rowNodes.set(entry.key, entry);
      trigger.addEventListener("click", () => {
        if (menuEntry === entry) closeMenu({ restoreFocus: true }); else openMenu(entry);
      });
      bindRowGestures(entry);
      item.append(button, time,
        element("span", `大人 ${row.adults}名 · 子供 ${row.children}名`, "row-party"),
        element("span", LABELS[row.status], `status-label ${row.status}`), trigger);
      list.insertBefore(item, list.children[index] ?? null);
      if (previous) list.removeChild(previous.item);
    });
  }

  async function refresh() {
    if (!started || paused || logoutPending || needsLogin || page !== "receptions") return false;
    // A valid, previously verified read-only view remains usable while polling.
    // Failed/expired/inconsistent responses still erase it below. Never use this
    // preserved menu as authorization for a future write operation.
    const background = canBrowse();
    abortReads({ preserveInteraction: background });
    const version = refreshVersion, authentication = authVersion;
    const current = () => started && !paused && !logoutPending && version === refreshVersion && authentication === authVersion;
    if (!background) {
      disabled("refresh-button", true);
      disabled("active-tab", true); disabled("recent-tab", true);
      if (get("staff-view")) get("staff-view").dataset.state = "loading";
      message("セッションと最新情報を確認しています…");
    }
    try {
      const began = now();
      const session = await request("/staff/session");
      if (!current()) return false;
      acceptSession(session, began);
      const active = await allPages("active", current);
      const recent = await allPages("recently_seated", current);
      if (!current()) return false;
      if (active.version !== recent.version
          || metadataSignature(active.metadata) !== metadataSignature(recent.metadata)) throw new ReadError("conflict");
      const activeIds = new Set(active.rows.map(row => row.id));
      if (recent.rows.some(row => activeIds.has(row.id))) throw new ReadError("invalid");
      const configurationChanged = displayedMetadata !== null
        && metadataSignature(displayedMetadata) !== metadataSignature(active.metadata);
      const nextRows = new Map([...active.rows.map(row => [`active:${row.id}`, row]),
        ...recent.rows.map(row => [`recently_seated:${row.id}`, row])]);
      const unchanged = entry => entry && !configurationChanged
        && nextRows.has(entry.key) && rowSignature(nextRows.get(entry.key)) === entry.signature;
      if ((menuEntry && !unchanged(menuEntry)) || (gesture && !unchanged(gesture.entry))) closeMenu();
      if (detailTarget && !unchanged(detailTarget)) closeDetail();
      const scroll = get("reception-scroll"), top = scroll?.scrollTop, left = scroll?.scrollLeft;
      renderRows("active-list", active.rows);
      renderRows("recent-list", recent.rows);
      // Restore the position at COMMIT time, not the position at request start:
      // staff may have scrolled while the network request was in progress.
      if (scroll && scroll.scrollTop !== top) scroll.scrollTop = top;
      if (scroll && scroll.scrollLeft !== left) scroll.scrollLeft = left;
      displayedMetadata = active.metadata; displayedVersion = active.version;
      for (const status of ["waiting", "called", "absent"]) text(`${status}-count`, String(active.rows.filter(row => row.status === status).length));
      hidden("active-empty", active.rows.length > 0); hidden("recent-empty", recent.rows.length > 0);
      hidden("staff-view", false);
      get("staff-view").dataset.state = "current";
      disabled("active-tab", false); disabled("recent-tab", false); applyTabs();
      text("updated-at", `${new Date(now()).toLocaleTimeString("ja-JP", { timeZone: "Asia/Tokyo" })} に全件取得 · 5秒ごとに更新`);
      message("");
      return true;
    } catch (error) {
      if (!current()) return false;
      if (error.code === "auth") {
        lockData("認証を確認できません。再ログインしてください。", { expired: true });
        env.location.replace("/staff/login");
      } else {
        clearData(); hidden("staff-view", true);
        if (!authenticated) csrf = "";
        message(error.code === "conflict" ? "取得中に受付情報が変わりました。次の更新で全件を取り直します。"
          : error.code === "incomplete" ? "全件を取得できないため、組数を表示していません。"
            : "最新情報を確認できません。通信とセッションを確認し、再取得してください。");
      }
      return false;
    } finally {
      if (version === refreshVersion) {
        disabled("refresh-button", needsLogin || logoutPending);
        schedulePoll();
      }
    }
  }

  async function openDetail(id) {
    if (!authenticated || paused || logoutPending || !UUID.test(id) || displayedVersion === null) return false;
    closeMenu(); closeDetail();
    detailReturnTarget = rowNodes.get(`${selectedTab}:${id}`)?.button ?? null;
    const version = detailVersion, authentication = authVersion, listVersion = displayedVersion;
    const current = () => started && !paused && !logoutPending && authenticated && version === detailVersion && authentication === authVersion;
    try {
      const result = await request(`/staff/receptions/${encodeURIComponent(id)}/operation-context`, { purpose: "detail" });
      if (!current()) return false;
      if (!validMetadata(result?.metadata) || !validRow(result.reception) || result.reception.id !== id) throw new ReadError("invalid");
      if (result.metadata.view_version !== listVersion || displayedVersion !== listVersion
          || metadataSignature(result.metadata) !== metadataSignature(displayedMetadata)) throw new ReadError("conflict");
      const row = result.reception, metadata = result.metadata;
      detailTarget = { key: `${selectedTab}:${id}`, signature: rowSignature(row) };
      const fields = element("dl", undefined, "detail-fields");
      const add = (label, value) => fields.append(element("dt", label), element("dd", String(value ?? "—")));
      add("受付番号", numberText(row.display_number)); add("受付日時", timeText(row.received_at));
      add("人数", `大人 ${row.adults}名 · 子供 ${row.children}名`); add("現在の状態", LABELS[row.status]);
      add("案内日時", timeText(row.seated_at)); add("実待ち時間", row.actual_wait_minutes === null ? "—" : `${row.actual_wait_minutes}分`);
      const extra = element("details");
      extra.append(element("summary", "管理用の参照情報"));
      const refs = element("dl", undefined, "detail-fields");
      const ref = (label, value) => refs.append(element("dt", label), element("dd", String(value ?? "—")));
      ref("受付ID", row.id); ref("受付順", row.reception_order); ref("revision", row.revision);
      ref("database_id", metadata.database_id); ref("write_epoch", metadata.write_epoch);
      ref("有効な案内イベントID", row.valid_seating_event?.event_id);
      ref("案内前の状態", LABELS[row.valid_seating_event?.from_status] ?? "—");
      extra.append(refs);
      get("detail-content").replaceChildren(fields, extra);
      text("detail-title", `${numberText(row.display_number)}の詳細`);
      hidden("detail-view", false); get("detail-title")?.focus();
      return true;
    } catch (error) {
      if (!current()) return false;
      closeDetail();
      if (error.code === "auth") {
        lockData("認証を確認できません。再ログインしてください。", { expired: true });
        env.location.replace("/staff/login");
      } else message(error.code === "conflict" ? "受付情報が変わりました。最新の一覧から詳細を選び直してください。" : "詳細を取得できません。再度選択してください。");
      return false;
    }
  }

  async function login(event) {
    event?.preventDefault();
    if (!started || page !== "login" || loginBusy || paused) return false;
    let credential = get("staff-token").value;
    get("staff-token").value = "";
    if (!credential) { message("共有スタッフ認証情報を入力してください。", true); return false; }
    loginBusy = true; disabled("login-button", true); disabled("staff-token", true);
    const version = ++authVersion;
    message("認証を確認しています…", true);
    try {
      const began = now();
      const result = await request("/staff/session", { method: "POST", headers: { "X-Staff-Token": credential } });
      credential = "";
      if (!started || version !== authVersion || paused) return false;
      absoluteDeadline = null; acceptSession(result, began);
      message("ログインしました。受付画面を開きます。", true);
      env.location.replace("/staff/");
      return true;
    } catch (error) {
      if (!started || version !== authVersion || paused) return false;
      csrf = "";
      message(error.code === "rate" ? "試行回数の上限です。1分以上おいて再試行してください。"
        : error.code === "auth" ? "認証できませんでした。認証情報と接続先を確認してください。"
          : "ログイン結果を確認できません。通信を確認して再試行してください。", true);
      return false;
    } finally {
      credential = ""; loginBusy = false;
      if (started && version === authVersion) { disabled("login-button", false); disabled("staff-token", false); }
    }
  }

  async function logout() {
    if (!started || logoutBusy || !authenticated && !logoutPending) return false;
    logoutPending = true; logoutBusy = true;
    logoutCsrf = csrf || logoutCsrf;
    lockData("ログアウトを確認しています…");
    disabled("logout-button", true);
    function confirmed() {
      logoutPending = false; logoutCsrf = ""; needsLogin = true;
      env.clearTimeout(expiryTimer);
      message("ログアウトしました。");
      env.location.replace("/staff/login");
      return true;
    }
    try {
      const result = await request("/staff/session", { method: "DELETE", headers: { "X-CSRF-Token": logoutCsrf } });
      if (!started) return false;
      if (result?.authenticated !== false) throw new ReadError("invalid");
      return confirmed();
    } catch (error) {
      if (!started) return false;
      if (error.code === "auth") {
        try {
          // DELETE 401 may also mean bad CSRF. Confirm with a safe session GET;
          // this check never restores receipt data or starts polling.
          const session = await request("/staff/session");
          if (!started) return false;
          if (validSession(session)) logoutCsrf = session.csrf_token;
        } catch (check) {
          if (!started) return false;
          if (check.status === 401) return confirmed();
        }
      }
      // A lost response does not prove that the server invalidated its session.
      message("ログアウト完了を確認できません。受付情報は非表示です。「ログアウトを再試行」を押してください。");
      text("logout-button", "ログアウトを再試行");
      disabled("logout-button", false);
      return false;
    } finally { logoutBusy = false; }
  }

  async function start() {
    started = true; paused = doc.visibilityState === "hidden";
    clearData(); hidden("staff-view", true);
    if (!secureOrigin()) {
      lockData("スタッフ専用のHTTPS接続で開いてください。", { expired: true });
      disabled("login-button", true); disabled("staff-token", true);
      return false;
    }
    if (paused || logoutPending) return false;
    if (page === "receptions") return refresh();
    const version = ++authVersion;
    try {
      const began = now();
      const result = await request("/staff/session");
      if (!started || version !== authVersion || paused) return false;
      acceptSession(result, began);
      env.location.replace("/staff/");
      return true;
    } catch (error) {
      if (!started || version !== authVersion || paused) return false;
      message(error.code === "auth" ? "共有スタッフ認証情報を入力してください。" : "認証状態を確認できません。接続を確認してください。", true);
      disabled("login-button", false); disabled("staff-token", false);
      return false;
    }
  }

  function pause() {
    paused = true; authVersion += 1; authenticated = false; csrf = "";
    abortReads(); clearData(); hidden("staff-view", true);
    if (get("staff-token")) get("staff-token").value = "";
    message("画面を再表示したときに認証と最新情報を確認します。", page === "login");
  }
  function resume() { if (started && !logoutPending) { paused = false; void start(); } }
  function visibility() { if (doc.visibilityState === "hidden") pause(); else resume(); }
  function submit(event) { void login(event); }
  function refreshClick() { void refresh(); }
  function logoutClick() { void logout(); }
  function activeTabClick() { selectTab("active"); }
  function recentTabClick() { selectTab("recently_seated"); }
  function tabKey(event) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || !canBrowse()) return;
    event.preventDefault();
    selectTab(event.key === "Home" ? "active" : event.key === "End" ? "recently_seated"
      : selectedTab === "active" ? "recently_seated" : "active", { focus: true });
  }
  function menuCloseClick() { closeMenu({ restoreFocus: true }); }
  function detailCloseClick() { closeDetail({ restoreFocus: true }); }
  function keyDown(event) {
    if (event.key !== "Escape") return;
    if (menuEntry) { event.preventDefault(); closeMenu({ restoreFocus: true }); }
    else if (get("detail-view") && !get("detail-view").hidden) { event.preventDefault(); closeDetail({ restoreFocus: true }); }
  }
  function pointerDown(event) {
    if (["touch", "pen"].includes(event.pointerType)) {
      pointers.add(event.pointerId);
      if (pointers.size > 1 || event.isPrimary === false) gesture = null;
    }
    if (menuEntry && !get("row-menu").contains(event.target) && !menuEntry.trigger.contains(event.target)) closeMenu();
  }
  function pointerEnd(event) {
    pointers.delete(event.pointerId);
    if (gesture && (event.type === "pointercancel" || !gesture.entry.item.contains(event.target))) {
      suppressClickUntil = now() + 500; gesture = null;
    }
  }
  function suppressClick(event) {
    if (event.detail !== 0 && now() < suppressClickUntil) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }
  function focusIn(event) {
    if (menuEntry && !get("row-menu").contains(event.target) && event.target !== menuEntry.trigger) closeMenu();
  }
  function listScroll() { closeMenu(); }
  get("login-form")?.addEventListener("submit", submit);
  get("refresh-button")?.addEventListener("click", refreshClick);
  get("logout-button")?.addEventListener("click", logoutClick);
  get("detail-close")?.addEventListener("click", detailCloseClick);
  get("active-tab")?.addEventListener("click", activeTabClick);
  get("recent-tab")?.addEventListener("click", recentTabClick);
  for (const id of ["active-tab", "recent-tab"]) get(id)?.addEventListener("keydown", tabKey);
  get("row-menu-close")?.addEventListener("click", menuCloseClick);
  get("reception-scroll")?.addEventListener("scroll", listScroll, { passive: true });
  doc.addEventListener("keydown", keyDown);
  doc.addEventListener("pointerdown", pointerDown, true);
  doc.addEventListener("pointerup", pointerEnd, true); doc.addEventListener("pointercancel", pointerEnd, true);
  doc.addEventListener("click", suppressClick, true); doc.addEventListener("focusin", focusIn);
  doc.addEventListener("visibilitychange", visibility);
  env.addEventListener("pagehide", pause); env.addEventListener("pageshow", resume);
  env.addEventListener("blur", closeMenu);

  function stop() {
    started = false; authVersion += 1; authenticated = false; csrf = ""; logoutCsrf = "";
    abortReads(); env.clearTimeout(expiryTimer); clearData(); hidden("staff-view", true);
    get("login-form")?.removeEventListener("submit", submit);
    get("refresh-button")?.removeEventListener("click", refreshClick);
    get("logout-button")?.removeEventListener("click", logoutClick);
    get("detail-close")?.removeEventListener("click", detailCloseClick);
    get("active-tab")?.removeEventListener("click", activeTabClick);
    get("recent-tab")?.removeEventListener("click", recentTabClick);
    for (const id of ["active-tab", "recent-tab"]) get(id)?.removeEventListener("keydown", tabKey);
    get("row-menu-close")?.removeEventListener("click", menuCloseClick);
    get("reception-scroll")?.removeEventListener("scroll", listScroll);
    doc.removeEventListener("keydown", keyDown);
    doc.removeEventListener("pointerdown", pointerDown, true);
    doc.removeEventListener("pointerup", pointerEnd, true); doc.removeEventListener("pointercancel", pointerEnd, true);
    doc.removeEventListener("click", suppressClick, true); doc.removeEventListener("focusin", focusIn);
    doc.removeEventListener("visibilitychange", visibility);
    env.removeEventListener("pagehide", pause); env.removeEventListener("pageshow", resume);
    env.removeEventListener("blur", closeMenu);
  }
  return { start, stop, refresh, login, logout, openDetail, closeDetail, selectTab };
}

if (typeof document !== "undefined" && document.body?.dataset.staffPage) {
  const app = createStaffApp({ document });
  void app.start();
}
