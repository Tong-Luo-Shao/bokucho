import { expect, test } from "bun:test";

import { reportedError, reportProblem } from "../../src/bokucho/web/assets/report.js";

test("the banner's headline and details go into the report", () => {
  expect(reportedError("發生了未預期的錯誤。", "KeyError: 'x'")).toBe(
    "發生了未預期的錯誤。\n\nKeyError: 'x'",
  );
  expect(reportedError("Wait for the running task to finish.", "")).toBe(
    "Wait for the running task to finish.",
  );
});

test("a report hands its text to the bridge and stays quiet when it opens", async () => {
  const sent = [];
  const shown = [];
  const bridge = { report_problem: async (text) => sent.push(text) };

  await reportProblem(bridge, "Boom", (err) => shown.push(err));

  expect(sent).toEqual(["Boom"]);
  expect(shown).toEqual([]);
});

test("a report that cannot open shows why", async () => {
  const failure = new Error("Opening links is unavailable.");
  const shown = [];
  const bridge = {
    report_problem: async () => {
      throw failure;
    },
  };

  await reportProblem(bridge, "", (err) => shown.push(err));

  expect(shown).toEqual([failure]);
});
