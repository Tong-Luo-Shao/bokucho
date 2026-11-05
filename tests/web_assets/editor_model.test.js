import { expect, test } from "bun:test";

import { insertFrame, paletteForImageMode } from "../../src/bokucho/web/assets/canvas_editor.js";

test.each(["1", "L", "LA"])("%s mode only exposes grayscale colors", (mode) => {
  const palette = paletteForImageMode(mode, []);

  expect(palette.length).toBeGreaterThan(1);
  for (const raw of palette) {
    const color = raw.replace("#", "");
    expect(color.slice(0, 2)).toBe(color.slice(2, 4));
    expect(color.slice(2, 4)).toBe(color.slice(4, 6));
  }
});

test("indexed mode preserves its exact source palette", () => {
  expect(paletteForImageMode("P", ["#102030ff", "#40506080"])).toEqual([
    "#102030ff",
    "#40506080",
  ]);
});

test.each([
  ["before the saved frame", 0, 1, 2],
  ["after the saved frame", 1, 0, 0],
  ["on the saved frame", 1, 1, 1],
])("a frame added %s leaves the same frame saved", (_where, active, output, expected) => {
  const model = { frames: ["a", "b"], activeFrame: active, outputFrame: output };

  insertFrame(model, "new");

  expect(model.frames[model.activeFrame]).toBe("new");
  expect(model.outputFrame).toBe(expected);
});
