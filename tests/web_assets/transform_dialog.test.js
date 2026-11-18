import { expect, test } from "bun:test";

import {
  oneCountAtATime,
  transformCountText,
  transformOptions,
} from "../../src/bokucho/web/assets/screens/transform_dialog.js";

const say = (text, values = {}) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]);

test("the dialog offers Simplified to Traditional with or without vocabulary", () => {
  expect(transformOptions("s2twp", say)).toEqual([
    { value: "s2twp", label: "Simplified → Traditional" },
    { value: "s2tw", label: "Simplified → Traditional, characters only" },
  ]);
});

test("a conversion saved by hand stays on offer", () => {
  const options = transformOptions("s2hk", say);
  expect(options.map((option) => option.value)).toEqual(["s2twp", "s2tw", "s2hk"]);
  expect(options[2].label).toBe("s2hk (from project.toml)");
});

// A count the test answers by hand, so it decides when each one returns.
function heldCounts() {
  const asked = [];
  const count = (choice) => new Promise((resolve, reject) => asked.push({ choice, resolve, reject }));
  return { asked, count };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test("choices made during a count wait for it, and only the last is counted", async () => {
  const { asked, count } = heldCounts();
  const shown = [];
  const counter = oneCountAtATime(count, { onCount: (n) => shown.push(n), onError: () => {} });

  counter.ask("s2twp");
  counter.ask("s2tw");
  counter.ask("s2hk");
  counter.ask(null);
  await settle();
  expect(asked.map(({ choice }) => choice)).toEqual(["s2twp"]);

  asked[0].resolve(6);
  await settle();
  expect(shown).toEqual([]);
  expect(asked.map(({ choice }) => choice)).toEqual(["s2twp", null]);

  asked[1].resolve(0);
  await settle();
  expect(shown).toEqual([0]);
  expect(asked).toHaveLength(2);
});

test("a failed count is reported only for the choice on screen", async () => {
  const { asked, count } = heldCounts();
  const errors = [];
  const counter = oneCountAtATime(count, { onCount: () => {}, onError: (err) => errors.push(err.message) });

  counter.ask("s2twp");
  counter.ask("s2hk");
  await settle();
  asked[0].reject(new Error("stale"));
  await settle();
  asked[1].reject(new Error("missing dictionary"));
  await settle();
  expect(errors).toEqual(["missing dictionary"]);
});

test("nothing is shown once the dialog has closed", async () => {
  const { asked, count } = heldCounts();
  const shown = [];
  const counter = oneCountAtATime(count, { onCount: (n) => shown.push(n), onError: () => {} });

  counter.ask("s2twp");
  await settle();
  counter.close();
  asked[0].resolve(6);
  await settle();
  expect(shown).toEqual([]);
});

test("the count says when there is nothing to convert", () => {
  expect(transformCountText(null, say)).toBe("Counting…");
  expect(transformCountText(0, say)).toBe("Nothing to convert.");
  expect(transformCountText(12, say)).toBe("12 line(s) will change.");
});
