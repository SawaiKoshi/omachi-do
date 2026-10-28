// No customer identity or staff credentials are used by this page.
import { createPredictionPreview } from "./prediction-preview.mjs";

const PENDING_KEY = "omachi.pending-reception.v1";
const REQUEST_TIMEOUT = 15_000;
const RECEIPT_TIMEOUT = 30_000;
const ENTRY_TIMEOUT = 60_000;
const QUEUE_INTERVAL = 5_000;
const RESTORE_MESSAGE = "前回の受付結果が確認できていません。同じ受付を再送して確認してください。";
const RETRY_MESSAGE = "受付結果を確認できませんでした。人数を変えずに「同じ受付を再送する」を押してください。";

function validParty(adults, children) {
  return Number.isInteger(adults) && Number.isInteger(children)
    && adults >= 0 && children >= 0 && adults + children >= 1 && adults + children <= 100;
}

function validPending(value) {
  return value && typeof value.key === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.key)
    && validParty(value.adults, value.children);
}

function validReceipt(value, pending) {
  return value && typeof value.display_number === "string" && value.display_number.length > 0
    && value.adults === pending.adults && value.children === pending.children
    && value.total_people === pending.adults + pending.children
    && Number.isInteger(value.snapshot?.waiting_groups) && value.snapshot.waiting_groups >= 0
    && value.prediction && Array.isArray(value.prediction.warnings)
    && value.prediction.warnings.every((warning) => typeof warning === "string");
}

export function formatNumber(value) {
  const match = /^(\d{4})(\d{2})(\d{2})-(\d+)$/.exec(value);
  return match ? `${match[1]}/${match[2]}/${match[3]} · ${match[4]}番` : value;
}

function validQueue(value) {
  return value && Number.isInteger(value.waiting_groups) && value.waiting_groups >= 0
    && typeof value.notice === "string" && Array.isArray(value.groups)
    && value.waiting_groups === value.groups.length
    && value.groups.every((group) => group && typeof group.display_number === "string"
      && group.display_number.length > 0 && Number.isInteger(group.total_people)
      && group.total_people >= 1 && group.total_people <= 100
      && ["waiting", "called", "absent"].includes(group.status))
    && new Set(value.groups.map((group) => group.display_number)).size === value.groups.length;
}

export function mountReception(doc, env = globalThis) {
  const get = (id) => doc.getElementById(id);
  const inputs = { adults: get("adults"), children: get("children") };
  const controls = Array.from(doc.querySelectorAll("[data-person]"));
  const submit = get("submit-button");
  const form = get("party-form");
  const now = () => env.now ? env.now() : Date.now();
  let pending = null;
  let busy = false;
  let blocked = false;
  let pageVersion = 0;
  let view = "home";
  let active = doc.visibilityState !== "hidden";
  let receiptDeadline = null;
  let receiptTimer = null;
  let entryDeadline = null;
  let entryTimer = null;
  let queue = null;
  let queueVersion = 0;
  let queueController = null;
  let queueTimer = null;
  let highlightTimer = null;
  const recentNumbers = new Map();
  const renderedLists = new Map();
  let homeAdults = 2;
  const preview = createPredictionPreview(env, renderPreview);

  function text(id, value) {
    if (get(id).textContent !== value) get(id).textContent = value;
  }

  function renderPreview(result) {
    const prefix = view === "home" ? "home" : "entry";
    get(`${prefix}-prediction`).dataset.state = result.state;
    if (view === "home") get("home-view").dataset.prediction = result.state;
    text(`${prefix}-prediction-label`, result.state === "idle" ? "この人数でのAI参考待ち時間"
      : view === "home" ? `大人${result.adults}名の場合のAI参考待ち時間`
        : `大人${result.adults}名・子供${result.children}名のAI参考待ち時間`);
    const value = result.state === "available" ? `約${Math.ceil(result.minutes)}分`
      : result.state === "loading" ? "参考待ち時間を確認しています…"
        : result.state === "pending" ? "受付結果を確認してください"
        : result.state === "idle" ? "人数を選ぶと目安が表示されます"
          : "現在、参考待ち時間を表示できません";
    text(`${prefix}-prediction-value`, value);
    text(`${prefix}-prediction-raw`, result.state === "available" ? `予測値：${result.minutes}分。大きな分表示は1分単位で切り上げています。` : "");
    get(`${prefix}-prediction-raw`).hidden = result.state !== "available";
    text(`${prefix}-prediction-updated`, result.predictedAt
      ? `${new Date(result.predictedAt).toLocaleTimeString("ja-JP", { timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit" })}取得${result.refreshing ? "・更新中" : ""}`
      : result.state === "error" ? "通信できません。受付はできます。" : "");
    const list = get(`${prefix}-prediction-warnings`);
    const warnings = result.warnings ?? [];
    if (list.dataset.warnings !== JSON.stringify(warnings)) {
      list.dataset.warnings = JSON.stringify(warnings);
      list.replaceChildren(...warnings.map((warning) => {
        const item = doc.createElement("li");
        item.textContent = warning;
        return item;
      }));
    }
  }

  function selectPreview(debounce = 0) {
    if (!active || busy || blocked || pending !== null) {
      preview.setActive(false);
      if (view === "entry" && (busy || pending !== null)) renderPreview({
        adults: pending?.adults ?? readCount(inputs.adults),
        children: pending?.children ?? readCount(inputs.children), state: "pending", warnings: [],
      });
      return;
    }
    if (view === "home") preview.select(homeAdults, 0, { repeat: true });
    else preview.select(readCount(inputs.adults), readCount(inputs.children), { debounce });
  }

  function message(text = "") {
    get("form-message").textContent = text;
    get("form-message").hidden = !text;
  }

  function readCount(input) {
    return /^(0|[1-9]\d*)$/.test(input.value) ? Number(input.value) : NaN;
  }

  function restorePending() {
    try {
      const saved = env.sessionStorage.getItem(PENDING_KEY);
      if (saved !== null) {
        const candidate = JSON.parse(saved);
        if (!validPending(candidate) || (pending !== null && (
          pending.key !== candidate.key || pending.adults !== candidate.adults || pending.children !== candidate.children
        ))) throw new Error("Conflicting pending reception");
        pending = { key: candidate.key, adults: candidate.adults, children: candidate.children };
        inputs.adults.value = String(pending.adults);
        inputs.children.value = String(pending.children);
        message(RESTORE_MESSAGE);
      } else if (pending !== null) {
        // Another document may have completed this reception while this page was cached.
        throw new Error("Pending reception changed");
      }
      return true;
    } catch {
      // Never replace an unknown or conflicting previous attempt with a new key.
      blocked = true;
      message("前回の受付情報を確認できません。新しく受付せず、スタッフにお声がけください。");
      return false;
    }
  }

  function refresh() {
    const adults = readCount(inputs.adults);
    const children = readCount(inputs.children);
    const valid = validParty(adults, children);
    const locked = busy || blocked || pending !== null;
    get("total-people").textContent = Number.isInteger(adults) && Number.isInteger(children)
      ? String(adults + children) : "—";
    for (const input of Object.values(inputs)) input.disabled = locked || view !== "entry";
    for (const button of controls) {
      const count = readCount(inputs[button.dataset.person]);
      button.disabled = locked || view !== "entry" || (button.dataset.delta === "-1" ? !(count > 0) : !valid || adults + children >= 100);
      // Both counts may be zero while the customer changes their party composition.
      if (!locked && view === "entry" && adults === 0 && children === 0 && button.dataset.delta === "1") button.disabled = false;
    }
    submit.disabled = view !== "entry" || busy || blocked || !valid;
    submit.textContent = busy ? "受付を確認しています…" : pending ? "同じ受付を再送する" : "この人数で受付する";
    form.setAttribute("aria-busy", String(busy));
    get("request-status").hidden = !busy;
    get("request-status").textContent = busy ? "通信中です。そのままお待ちください。" : "";
    get("start-button").disabled = busy || blocked;
    get("back-button").disabled = locked;
    if (!locked) message(valid ? "" : "大人・子供それぞれ0以上の整数で、合計1〜100人にしてください。");
  }

  function clearReceipt() {
    env.clearTimeout(receiptTimer);
    receiptDeadline = null;
    receiptTimer = null;
    for (const id of ["receipt-number", "receipt-people"]) get(id).textContent = "";
    get("receipt-view").hidden = true;
  }

  function cancelEntryTimer() {
    env.clearTimeout(entryTimer);
    entryTimer = null;
    entryDeadline = null;
  }

  function showHome(focus = true) {
    cancelEntryTimer();
    view = "home";
    get("home-view").hidden = false;
    get("entry-view").hidden = true;
    inputs.adults.value = "1";
    inputs.children.value = "0";
    homeAdults = 2;
    get("preview-two").setAttribute("aria-pressed", "true");
    get("preview-four").setAttribute("aria-pressed", "false");
    message();
    refresh();
    if (active) env.scrollTo?.({ top: 0, left: 0, behavior: "auto" });
    if (focus && active) get("home-title").focus();
    if (!busy) { preview.setActive(active); selectPreview(); }
  }

  function armEntryTimer() {
    cancelEntryTimer();
    if (view !== "entry" || pending !== null || busy || blocked) return;
    entryDeadline = now() + ENTRY_TIMEOUT;
    entryTimer = env.setTimeout(() => {
      if (view === "entry" && pending === null && !busy && !blocked) showHome();
    }, ENTRY_TIMEOUT);
  }

  function openEntry(focus = true) {
    clearReceipt();
    view = "entry";
    get("home-view").hidden = true;
    get("entry-view").hidden = false;
    refresh();
    armEntryTimer();
    if (focus && active) get("entry-title").focus();
    selectPreview(400);
  }

  function renderQueue() {
    if (queue === null) return;
    text("queue-count", String(queue.waiting_groups));
    get("queue-empty").hidden = queue.groups.length !== 0;
    const statusLabels = { waiting: "受付待機中", called: "呼出中", absent: "不在" };
    function row(group) {
      const item = doc.createElement("li");
      const recent = (recentNumbers.get(group.display_number) ?? 0) > now();
      item.className = `queue-row${recent ? " recent" : ""}`;
      item.dataset.number = group.display_number;
      const number = doc.createElement("span");
      number.className = "queue-number";
      const match = /^(\d{4})(\d{2})(\d{2})-(\d+)$/.exec(group.display_number);
      const digits = doc.createElement("span");
      digits.className = "number-digits";
      digits.textContent = match ? `${match[4]}番` : group.display_number;
      number.append(digits);
      if (match) {
        const date = doc.createElement("span");
        date.className = "number-date";
        date.textContent = `${match[1]}/${match[2]}/${match[3]}受付`;
        number.append(date);
      }
      item.append(number);
      for (const [className, content] of [
        ["queue-people", `${group.total_people}人`],
        ["queue-status", statusLabels[group.status]],
      ]) {
        const part = doc.createElement("span");
        part.className = className;
        part.textContent = content;
        item.append(part);
      }
      if (recent) {
        const badge = doc.createElement("span");
        badge.className = "recent-label";
        badge.textContent = "新しい受付";
        item.append(badge);
      }
      return item;
    }
    function renderList(id, groups) {
      const signature = (group) => JSON.stringify([
        group.display_number, group.total_people, group.status, (recentNumbers.get(group.display_number) ?? 0) > now(),
      ]);
      const signatures = groups.map(signature);
      const previous = renderedLists.get(id);
      if (previous && JSON.stringify(signatures) === previous.signature) return;
      const nodes = new Map();
      const items = groups.map((group, index) => {
        const prior = previous?.nodes.get(group.display_number);
        const node = prior?.signature === signatures[index] ? prior.node : row(group);
        nodes.set(group.display_number, { signature: signatures[index], node });
        return node;
      });
      get(id).replaceChildren(...items);
      renderedLists.set(id, { signature: JSON.stringify(signatures), nodes });
    }
    const scrollTop = get("queue-scroll").scrollTop;
    renderList("queue-list", queue.groups);
    for (const [id, status] of [["called-list", "called"], ["absent-list", "absent"]]) {
      const groups = queue.groups.filter((group) => group.status === status);
      renderList(id, groups);
      get(status === "called" ? "called-panel" : "absent-panel").hidden = groups.length === 0;
    }
    get("queue-scroll").scrollTop = scrollTop;
  }

  function expireHighlights() {
    env.clearTimeout(highlightTimer);
    for (const [number, deadline] of recentNumbers) if (deadline <= now()) recentNumbers.delete(number);
    renderQueue();
    if (recentNumbers.size > 0) {
      highlightTimer = env.setTimeout(expireHighlights, Math.max(0, Math.min(...recentNumbers.values()) - now()));
    }
  }

  function stopQueueUpdates() {
    queueVersion += 1;
    env.clearTimeout(queueTimer);
    queueTimer = null;
    if (queueController !== null) queueController.abort();
    queueController = null;
  }

  async function updateQueue() {
    if (!active || queueController !== null) return;
    env.clearTimeout(queueTimer);
    const version = queueVersion;
    const controller = new AbortController();
    queueController = controller;
    const timeout = env.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
    try {
      const response = await env.fetch("/public/queue", {
        method: "GET", signal: controller.signal, credentials: "same-origin", cache: "no-store", redirect: "error",
      });
      if (response.status !== 200) throw new Error("Queue unavailable");
      const result = await response.json();
      if (!validQueue(result)) throw new Error("Invalid public queue");
      if (version !== queueVersion || !active) return;
      queue = result;
      preview.setQueueEmpty(queue.waiting_groups === 0);
      renderQueue();
      get("queue-message").textContent = "";
      get("queue-message").hidden = true;
      get("queue-updated").textContent = `最終更新：${new Date(now()).toLocaleTimeString("ja-JP")}`;
    } catch {
      if (version === queueVersion && active) {
        get("queue-message").textContent = "待ち状況を更新できません。表示中の情報は最新ではありません。";
        get("queue-message").hidden = false;
      }
    } finally {
      env.clearTimeout(timeout);
      if (queueController === controller) queueController = null;
      if (version === queueVersion && active) queueTimer = env.setTimeout(updateQueue, QUEUE_INTERVAL);
    }
  }

  function refreshQueueNow() {
    // Discard an older GET that may have captured the queue before a successful POST.
    stopQueueUpdates();
    void updateQueue();
  }

  function showReceipt(receipt) {
    clearReceipt();
    showHome(false);
    if (!active) return;
    get("receipt-number").textContent = formatNumber(receipt.display_number);
    get("receipt-people").textContent = `${receipt.total_people}人`;
    get("receipt-view").hidden = false;
    receiptDeadline = now() + RECEIPT_TIMEOUT;
    receiptTimer = env.setTimeout(clearReceipt, RECEIPT_TIMEOUT);
    recentNumbers.set(receipt.display_number, receiptDeadline);
    expireHighlights();
    // Announce the notice while keeping the next customer's start button in view.
    get("receipt-title").focus({ preventScroll: true });
  }

  async function send(event) {
    event.preventDefault();
    if (busy || blocked || view !== "entry") return;
    // A history-restored document can hold older state than sessionStorage.
    if (!restorePending()) { refresh(); return; }
    const adults = readCount(inputs.adults);
    const children = readCount(inputs.children);
    if (!validParty(adults, children)) { refresh(); return; }
    if (pending === null) {
      try {
        const candidate = { key: env.crypto.randomUUID(), adults, children };
        // Persist before sending: a reload after the server commits must use the same key.
        env.sessionStorage.setItem(PENDING_KEY, JSON.stringify(candidate));
        pending = candidate;
      } catch {
        blocked = true;
        message("再送に必要な情報を端末に保持できません。スタッフにお声がけください。");
        refresh();
        return;
      }
    }
    busy = true;
    selectPreview();
    cancelEntryTimer();
    message();
    refresh();
    const controller = new AbortController();
    const requestPageVersion = pageVersion;
    const timeout = env.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
    try {
      const response = await env.fetch("/receptions", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": pending.key },
        body: JSON.stringify({ adults: pending.adults, children: pending.children }),
        signal: controller.signal,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
      });
      if (response.status !== 201 && response.status !== 200) {
        message(response.status === 409 || response.status === 422
          ? "受付内容を確認する必要があります。再度新しい受付をせず、スタッフにお声がけください。"
          : RETRY_MESSAGE);
        return;
      }
      const receipt = await response.json();
      if (!validReceipt(receipt, pending)) throw new Error("Invalid receipt");
      if (requestPageVersion !== pageVersion) throw new Error("Page left during request");
      // Store no receipt, reception number, or history in browser storage.
      env.sessionStorage.removeItem(PENDING_KEY);
      pending = null;
      showReceipt(receipt);
      refreshQueueNow();
    } catch {
      message(RETRY_MESSAGE);
    } finally {
      env.clearTimeout(timeout);
      busy = false;
      refresh();
      if (pending === null && !blocked) { preview.setActive(active); selectPreview(); }
    }
  }

  for (const input of Object.values(inputs)) input.addEventListener("input", () => { refresh(); armEntryTimer(); selectPreview(400); });
  for (const button of controls) button.addEventListener("click", () => {
    if (button.disabled) return;
    const input = inputs[button.dataset.person];
    input.value = String(readCount(input) + Number(button.dataset.delta));
    refresh();
    armEntryTimer();
    selectPreview(400);
  });
  form.addEventListener("submit", send);
  get("start-button").addEventListener("click", () => {
    if (busy || blocked) return;
    restorePending();
    openEntry();
  });
  get("back-button").addEventListener("click", () => {
    if (busy || blocked || pending !== null) return;
    if (!restorePending() || pending !== null) { openEntry(); return; }
    showHome();
  });
  get("next-button").addEventListener("click", clearReceipt);
  for (const [id, adults] of [["preview-two", 2], ["preview-four", 4]]) {
    get(id).disabled = false;
    get(id).addEventListener("click", () => {
      if (homeAdults === adults) return;
      homeAdults = adults;
      get("preview-two").setAttribute("aria-pressed", String(adults === 2));
      get("preview-four").setAttribute("aria-pressed", String(adults === 4));
      selectPreview();
    });
  }
  for (const event of ["pointerdown", "keydown"]) doc.addEventListener(event, armEntryTimer);

  function leavePage() {
    active = false;
    preview.setActive(false);
    clearReceipt();
    recentNumbers.clear();
    expireHighlights();
    stopQueueUpdates();
    if (!busy && pending === null && !blocked) showHome(false);
  }

  function resumePage() {
    active = doc.visibilityState !== "hidden";
    if (receiptDeadline !== null && now() >= receiptDeadline) clearReceipt();
    expireHighlights();
    if (!busy) {
      restorePending();
      if (pending !== null || blocked) openEntry(false);
      else if (entryDeadline !== null && now() >= entryDeadline) showHome(false);
      refresh();
    }
    void updateQueue();
    preview.setActive(active && !busy && !blocked && pending === null);
    selectPreview(view === "entry" ? 400 : 0);
  }

  env.addEventListener("pagehide", () => { pageVersion += 1; leavePage(); });
  env.addEventListener("pageshow", resumePage);
  doc.addEventListener("visibilitychange", () => {
    if (doc.visibilityState === "hidden") leavePage(); else resumePage();
  });
  inputs.adults.value = "1";
  inputs.children.value = "0";
  restorePending();
  if (pending !== null || blocked) openEntry(false);
  refresh();
  selectPreview(view === "entry" ? 400 : 0);
  void updateQueue();
}

if (typeof document !== "undefined") mountReception(document);
