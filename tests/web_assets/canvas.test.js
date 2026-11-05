import { expect, test } from "bun:test";

import { paintBrushPixels } from "../../src/bokucho/web/assets/image_editor/canvas.js";

test("painting one pixel preserves neighboring pixels", () => {
  const pixels = new Uint8ClampedArray([
    255, 0, 0, 255,
    0, 255, 0, 255,
    0, 0, 255, 255,
  ]);

  paintBrushPixels(pixels, 3, 1, 1, 0, { r: 10, g: 20, b: 30, a: 128 }, 1, false);

  expect([...pixels]).toEqual([
    255, 0, 0, 255,
    10, 20, 30, 128,
    0, 0, 255, 255,
  ]);
});
