import { expect, test } from "bun:test";

import { canBuild, gameFontOptions } from "../../src/bokucho/web/assets/screens/release.js";

const t = (_context, text, values = {}) =>
  text.replace(/\{(\w+)\}/g, (_, key) => values[key]);

const UNIFONT = { path: "C:/Windows/Fonts/unifont-all.ttf", name: "Unifont Smooth Medium" };
const NOTO = { path: "C:/Fonts/NotoSansTC.ttf", name: "Noto Sans TC" };

test("a font has to be chosen: the placeholder cannot be", () => {
  expect(gameFontOptions(null, [NOTO], t)).toEqual([
    { value: "", label: "Choose a font", disabled: true },
    { value: NOTO.path, label: NOTO.name },
  ]);
});

test("the chosen font is offered before the list arrives, and once after", () => {
  expect(gameFontOptions(UNIFONT, null, t).map((option) => option.value)).toEqual([
    "",
    UNIFONT.path,
  ]);
  expect(gameFontOptions(UNIFONT, [NOTO, UNIFONT], t).map((option) => option.value)).toEqual([
    "",
    NOTO.path,
    UNIFONT.path,
  ]);
});

test("a LiveMaker release waits for its game text font; other engines do not", () => {
  expect(canBuild("livemaker", { font: null })).toBe(false);
  expect(canBuild("livemaker", { font: UNIFONT })).toBe(true);
  expect(canBuild("renpy", { font: null })).toBe(true);
});
