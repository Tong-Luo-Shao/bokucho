// Translation events are sparse while a provider request is in flight. Keep a
// clock anchor beside the last backend sample so elapsed time and ETA stay live.

const NUMBER_FIELDS = [
  "processed",
  "total",
  "translated",
  "failed",
  "batches",
  "lanes",
  "requests",
  "input_tokens",
  "output_tokens",
  "cached_input_tokens",
  "total_tokens",
  "estimated_input_tokens",
];

function numberOr(value, fallback) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

export function createTranslationRun(startedAt = Date.now()) {
  return {
    status: "running",
    startedAt,
    lastUpdateAt: startedAt,
    elapsed_seconds: 0,
    processed: 0,
    total: 0,
    translated: 0,
    failed: 0,
    batches: 0,
    lanes: 0,
    requests: 0,
    input_tokens: 0,
    output_tokens: 0,
    cached_input_tokens: 0,
    total_tokens: 0,
    estimated_input_tokens: 0,
    token_usage_reported: false,
    // Characters of the replies still arriving; 0 once their batch lands.
    streamed_chars: 0,
    detail: "",
    // What the provider said when the run failed, already redacted.
    error: "",
  };
}

export function translationTiming(run, now = Date.now()) {
  if (!run) {
    return { elapsedSeconds: null, etaSeconds: null, unitsPerMinute: null };
  }
  const liveSeconds = run.status === "running"
    ? Math.max(0, now - run.lastUpdateAt) / 1_000
    : 0;
  const elapsedSeconds = Math.max(0, run.elapsed_seconds + liveSeconds);
  const processed = Math.max(0, run.processed);
  const total = Math.max(0, run.total);
  const unitsPerMinute = processed > 0 && elapsedSeconds > 0
    ? (processed / elapsedSeconds) * 60
    : null;
  let etaSeconds = null;
  if (total > 0 && processed >= total) {
    etaSeconds = 0;
  } else if (processed > 0 && total > processed) {
    etaSeconds = elapsedSeconds * (total - processed) / processed;
  }
  return { elapsedSeconds, etaSeconds, unitsPerMinute };
}

export function updateTranslationRun(run, event, receivedAt = Date.now()) {
  if (event.job !== "translate") return run;
  const current = run ?? createTranslationRun(receivedAt);
  const metrics = event.type === "progress" ? (event.metrics ?? {}) : (event.detail ?? {});
  const elapsedAtEvent = translationTiming(current, receivedAt).elapsedSeconds ?? 0;
  const updated = {
    ...current,
    lastUpdateAt: receivedAt,
    elapsed_seconds: numberOr(metrics.elapsed_seconds, elapsedAtEvent),
  };
  for (const field of NUMBER_FIELDS) {
    updated[field] = numberOr(metrics[field], current[field]);
  }
  if (event.current !== null && event.current !== undefined) {
    updated.processed = numberOr(event.current, updated.processed);
  }
  if (event.total !== null && event.total !== undefined) {
    updated.total = numberOr(event.total, updated.total);
  }
  if (typeof metrics.token_usage_reported === "boolean") {
    updated.token_usage_reported = metrics.token_usage_reported;
  }
  updated.streamed_chars = event.type === "progress" && event.transient
    ? numberOr(metrics.streamed_chars, 0)
    : 0;
  if (event.type === "progress") {
    // A batch sent before the pause still reports in; the run stays paused.
    if (current.status !== "paused") updated.status = "running";
    if (typeof event.detail === "string") updated.detail = event.detail;
  } else if (["succeeded", "failed", "cancelled", "paused"].includes(event.type)) {
    // A paused run is not live, so translationTiming stops adding to it.
    updated.status = event.type;
    if (event.type === "failed") updated.error = event.message ?? "";
  } else if (event.type === "resumed") {
    updated.status = "running";
  }
  return updated;
}

export function tokenUsage(run) {
  if (!run) return { tokens: 0, estimated: false };
  if (run.token_usage_reported) {
    const reported = run.total_tokens || run.input_tokens + run.output_tokens;
    return { tokens: reported, estimated: false };
  }
  return { tokens: run.estimated_input_tokens, estimated: true };
}

export function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "--";
  const rounded = Math.max(0, Math.round(seconds));
  const hours = Math.floor(rounded / 3_600);
  const minutes = Math.floor((rounded % 3_600) / 60);
  const remainder = rounded % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  }
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}
