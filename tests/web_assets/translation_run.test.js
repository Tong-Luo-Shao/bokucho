import { describe, expect, test } from "bun:test";

import {
  createTranslationRun,
  tokenUsage,
  translationTiming,
  updateTranslationRun,
} from "../../src/bokucho/web/assets/translation_run.js";

describe("translation run metrics", () => {
  test("recomputes elapsed time and ETA between provider events", () => {
    const started = createTranslationRun(1_000);
    const progress = updateTranslationRun(started, {
      type: "progress",
      job: "translate",
      current: 2,
      total: 10,
      detail: "script.txt",
      metrics: {
        elapsed_seconds: 4,
        batches: 1,
        input_tokens: 100,
        output_tokens: 25,
        cached_input_tokens: 40,
        token_usage_reported: true,
      },
    }, 5_000);

    expect(translationTiming(progress, 7_000)).toEqual({
      elapsedSeconds: 6,
      etaSeconds: 24,
      unitsPerMinute: 20,
    });
    expect(tokenUsage(progress)).toEqual({ tokens: 125, estimated: false });
  });

  test("preserves final totals after the run completes", () => {
    const running = createTranslationRun(1_000);
    const completed = updateTranslationRun(running, {
      type: "succeeded",
      job: "translate",
      detail: {
        processed: 10,
        total: 10,
        translated: 9,
        failed: 1,
        elapsed_seconds: 12,
        estimated_input_tokens: 300,
        token_usage_reported: false,
      },
    }, 13_000);

    expect(completed.status).toBe("succeeded");
    expect(translationTiming(completed, 20_000).etaSeconds).toBe(0);
    expect(tokenUsage(completed)).toEqual({ tokens: 300, estimated: true });
  });

  test("stops the clock while paused and starts it again on resume", () => {
    const running = createTranslationRun(1_000);
    const paused = updateTranslationRun(running, { type: "paused", job: "translate" }, 5_000);

    expect(paused.status).toBe("paused");
    expect(translationTiming(paused, 65_000).elapsedSeconds).toBe(4);

    const resumed = updateTranslationRun(paused, { type: "resumed", job: "translate" }, 65_000);

    expect(resumed.status).toBe("running");
    expect(translationTiming(resumed, 67_000).elapsedSeconds).toBe(6);
  });

  test("stays paused while a batch already sent reports in", () => {
    const paused = updateTranslationRun(
      createTranslationRun(1_000),
      { type: "paused", job: "translate" },
      5_000,
    );
    const late = updateTranslationRun(paused, {
      type: "progress",
      job: "translate",
      current: 3,
      total: 10,
      metrics: { elapsed_seconds: 4 },
    }, 6_000);

    expect(late.status).toBe("paused");
    expect(late.processed).toBe(3);
  });

  test("keeps the provider's error when a run fails, and drops it on the next start", () => {
    const failed = updateTranslationRun(createTranslationRun(1_000), {
      type: "failed",
      job: "translate",
      message: "429 RESOURCE_EXHAUSTED: quota exceeded",
    }, 5_000);

    expect(failed.status).toBe("failed");
    expect(failed.error).toBe("429 RESOURCE_EXHAUSTED: quota exceeded");
    expect(createTranslationRun(6_000).error).toBe("");
  });

  test("counts the reply in flight until the batch lands", () => {
    const streaming = updateTranslationRun(createTranslationRun(1_000), {
      type: "progress",
      job: "translate",
      current: 0,
      total: 4,
      message: "translate.streaming",
      transient: true,
      metrics: { streamed_chars: 120 },
    }, 2_000);
    expect(streaming.streamed_chars).toBe(120);

    const landed = updateTranslationRun(streaming, {
      type: "progress",
      job: "translate",
      current: 2,
      total: 4,
      message: "translate.batch.completed",
      metrics: {},
    }, 3_000);
    expect(landed.streamed_chars).toBe(0);

    const cancelled = updateTranslationRun(streaming, {
      type: "cancelled",
      job: "translate",
    }, 3_000);
    expect(cancelled.streamed_chars).toBe(0);
  });
});
