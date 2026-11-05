import { expect, test } from "bun:test";

import {
  inventoryMayHaveChanged,
  rescanChoices,
} from "../../src/bokucho/web/assets/screens/images.js";

const t = (_context, text, values = {}) =>
  text.replace(/\{(\w+)\}/g, (_, key) => values[key]);

test("a picture is scanned again at the settings' threshold or at a lower one", () => {
  const choices = rescanChoices(0.9, t);
  expect(choices.map(({ key, threshold }) => [key, threshold])).toEqual([
    ["cancel", undefined],
    ["settings", 0.9],
    ["lower", 0.5],
  ]);
  expect(choices.find((choice) => choice.primary).key).toBe("lower");
  expect(choices[1].label).toBe("Use OCR settings (threshold 0.9)");
});

test("settings already at or under the lower threshold offer no lower one", () => {
  const choices = rescanChoices(0.4, t);
  expect(choices.map((choice) => choice.key)).toEqual(["cancel", "settings"]);
  expect(choices.find((choice) => choice.primary).key).toBe("settings");
});

test("the list is read again however an images job ended", () => {
  for (const type of ["succeeded", "failed", "cancelled"]) {
    expect(inventoryMayHaveChanged({ type, job: "images-ocr-rescan" })).toBe(true);
  }
  expect(inventoryMayHaveChanged({ type: "progress", job: "images-ocr-rescan" })).toBe(false);
  expect(inventoryMayHaveChanged({ type: "cancelled", job: "translate" })).toBe(false);
});
