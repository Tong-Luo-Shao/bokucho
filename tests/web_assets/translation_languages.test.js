import { expect, test } from "bun:test";

import { startTranslation } from "../../src/bokucho/web/assets/screens/translation_dashboard.js";

test("the selected languages are saved before translation starts", async () => {
  const calls = [];
  let finishSave;
  const ctx = {
    appState: { project: { source_lang: "ja", target_lang: "zh-Hant-TW" } },
    bridge: {
      save_project_settings(...args) {
        calls.push(["save", ...args]);
        return new Promise((resolve) => { finishSave = resolve; });
      },
    },
    async startJob(...args) {
      calls.push(["start", ...args]);
      return true;
    },
  };
  const pending = startTranslation(ctx, { source_lang: "und", target_lang: "ko" });
  expect(calls).toEqual([["save", null, "und", "ko"]]);
  finishSave({ source_lang: "und", target_lang: "ko" });
  expect(await pending).toBe(true);
  expect(calls[1]).toEqual(["start", "translate", undefined]);
  expect(ctx.appState.project).toEqual({ source_lang: "und", target_lang: "ko" });
});

test("a failed language save does not start a translation", async () => {
  let started = false;
  const ctx = {
    appState: { project: { source_lang: "ja", target_lang: "en" } },
    bridge: { async save_project_settings() { throw new Error("Cannot save"); } },
    async startJob() { started = true; },
  };
  await expect(startTranslation(ctx, { source_lang: "und", target_lang: "ko" }))
    .rejects.toThrow("Cannot save");
  expect(started).toBe(false);
  expect(ctx.appState.project).toEqual({ source_lang: "ja", target_lang: "en" });
});

