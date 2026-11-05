import { expect, test } from "bun:test";

import { pointerToolForButton } from "../../src/bokucho/web/assets/canvas_editor.js";

test("middle mouse always resolves to canvas panning", () => {
  expect(pointerToolForButton("pencil", 1)).toBe("hand");
  expect(pointerToolForButton("fill", 1)).toBe("hand");
  expect(pointerToolForButton("text", 1)).toBe("hand");
});

test("primary and secondary buttons keep the selected tool", () => {
  expect(pointerToolForButton("pencil", 0)).toBe("pencil");
  expect(pointerToolForButton("pencil", 2)).toBe("pencil");
});

test("canvas pointer routing uses the button-specific tool", async () => {
  const source = await Bun.file(
    new URL("../../src/bokucho/web/assets/canvas_editor.js", import.meta.url),
  ).text();

  expect(source).toContain("pointerToolForButton(model.tool, event.button)");
});
