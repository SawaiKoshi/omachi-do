// Read-only predictions share a short cache; reception submission never waits for this module.
export const PREVIEW_TTL = 60_000;
const TIMEOUT = 15_000;
const NO_QUEUE_WARNING = "待ち組数が0でも、すぐのご案内や待ち時間0分を保証するものではありません。";
const STATUSES = new Set([
  "available", "model_unavailable", "no_waiting_groups", "unsupported_weekday",
  "unsupported_time", "prediction_failed",
]);

function validResult(value) {
  const prediction = value?.prediction;
  return prediction && STATUSES.has(prediction.status)
    && typeof prediction.predicted_at === "string" && Number.isFinite(Date.parse(prediction.predicted_at))
    && Array.isArray(prediction.warnings) && prediction.warnings.every((item) => typeof item === "string")
    && Number.isInteger(value.snapshot?.waiting_groups) && value.snapshot.waiting_groups >= 0
    && (prediction.status === "available"
      ? typeof prediction.minutes === "number" && Number.isFinite(prediction.minutes)
        && prediction.minutes >= 0 && value.snapshot.waiting_groups > 0
      : prediction.minutes === null);
}

export function createPredictionPreview(env, onChange) {
  const now = () => env.now ? env.now() : Date.now();
  const cache = new Map();
  let active = true;
  let target = null;
  let request = null;
  let timer = null;
  let emptyQueue = false;

  function cancelTimer() {
    env.clearTimeout(timer);
    timer = null;
  }

  function cancelRequest() {
    if (request !== null) request.controller.abort();
    request = null;
  }

  function emit(state, data = {}) {
    if (active && target) onChange({ ...target, state, ...data });
  }

  function repeatAfter(delay) {
    cancelTimer();
    if (active && target?.repeat && !emptyQueue) timer = env.setTimeout(run, delay);
  }

  async function run() {
    cancelTimer();
    if (!active || !target) return;
    if (target.key === null) { emit("idle", { warnings: [] }); return; }
    if (emptyQueue) { emit("unavailable", { warnings: [NO_QUEUE_WARNING] }); return; }
    const saved = cache.get(target.key);
    if (saved && saved.expiresAt > now()) {
      emit(saved.state, saved);
      repeatAfter(saved.expiresAt - now());
      return;
    }
    if (request?.key === target.key) return;
    cancelRequest();
    const current = { key: target.key, controller: new AbortController() };
    const { adults, children } = target;
    request = current;
    if (saved?.state === "available" && now() < saved.expiresAt + TIMEOUT) {
      // Keep the same party's last reference visible during its periodic refresh.
      emit("available", { ...saved, refreshing: true });
    } else emit("loading");
    const timeout = env.setTimeout(() => current.controller.abort(), TIMEOUT);
    try {
      const response = await env.fetch("/predict", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ adults, children }), signal: current.controller.signal,
        credentials: "same-origin", cache: "no-store", redirect: "error",
      });
      if (response.status !== 200) throw new Error("Preview unavailable");
      const value = await response.json();
      if (!validResult(value)) throw new Error("Invalid preview");
      if (current.controller.signal.aborted || request !== current) return;
      const prediction = value.prediction;
      const saved = {
        state: prediction.status === "available" ? "available" : "unavailable",
        minutes: prediction.minutes, warnings: prediction.warnings,
        predictedAt: prediction.predicted_at, expiresAt: now() + PREVIEW_TTL,
      };
      cache.delete(current.key);
      cache.set(current.key, saved);
      if (cache.size > 32) cache.delete(cache.keys().next().value);
      if (active && target?.key === current.key) emit(saved.state, saved);
    } catch {
      if (request === current && active && target?.key === current.key) emit("error", { warnings: [] });
    } finally {
      env.clearTimeout(timeout);
      if (request === current) {
        request = null;
        repeatAfter(PREVIEW_TTL);
      }
    }
  }

  function select(adults, children, { debounce = 0, repeat = false } = {}) {
    const valid = Number.isInteger(adults) && Number.isInteger(children)
      && adults >= 0 && children >= 0 && adults + children >= 1 && adults + children <= 100;
    cancelTimer();
    if (!valid) {
      cancelRequest();
      target = { adults, children, repeat: false, key: null };
      emit("idle", { warnings: [] });
      return;
    }
    const key = `${adults}:${children}`;
    if (request && request.key !== key) cancelRequest();
    target = { adults, children, key, repeat };
    if (!active) return;
    if (emptyQueue) { emit("unavailable", { warnings: [NO_QUEUE_WARNING] }); return; }
    const saved = cache.get(key);
    if (saved && saved.expiresAt > now()) {
      emit(saved.state, saved);
      repeatAfter(saved.expiresAt - now());
    } else {
      if (debounce > 0 && request?.key !== key) {
        emit("loading");
        timer = env.setTimeout(run, debounce);
      }
      else void run();
    }
  }

  return {
    select,
    setActive(value) {
      active = value;
      cancelTimer();
      if (!value) cancelRequest();
    },
    setQueueEmpty(value) {
      if (value === emptyQueue) return;
      emptyQueue = value;
      cache.clear();
      cancelTimer();
      cancelRequest();
      if (target && active) {
        if (target.key === null) emit("idle", { warnings: [] });
        else if (value) emit("unavailable", { warnings: [NO_QUEUE_WARNING] });
        else void run();
      }
    },
  };
}
