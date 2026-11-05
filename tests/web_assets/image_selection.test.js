import { expect, test } from "bun:test";

import { ignoreAction, nextSelection } from "../../src/bokucho/web/assets/screens/images.js";

const ORDER = ["a.png", "b.png", "c.png", "d.png"];

test("a plain click selects that picture alone", () => {
  expect(nextSelection(ORDER, ["a.png", "b.png"], "a.png", "c.png")).toEqual({
    selected: ["c.png"],
    anchor: "c.png",
  });
});

test("Ctrl adds a picture to the selection, and takes a selected one out", () => {
  expect(nextSelection(ORDER, ["a.png"], "a.png", "c.png", { ctrl: true })).toEqual({
    selected: ["a.png", "c.png"],
    anchor: "c.png",
  });
  expect(nextSelection(ORDER, ["a.png", "c.png"], "c.png", "a.png", { ctrl: true })).toEqual({
    selected: ["c.png"],
    anchor: "a.png",
  });
});

test("Shift selects the run from the last click, in either direction", () => {
  expect(nextSelection(ORDER, ["b.png"], "b.png", "d.png", { shift: true })).toEqual({
    selected: ["b.png", "c.png", "d.png"],
    anchor: "b.png",
  });
  expect(nextSelection(ORDER, ["c.png"], "c.png", "a.png", { shift: true })).toEqual({
    selected: ["a.png", "b.png", "c.png"],
    anchor: "c.png",
  });
});

test("Shift with nothing clicked yet selects just that picture", () => {
  expect(nextSelection(ORDER, [], null, "b.png", { shift: true })).toEqual({
    selected: ["b.png"],
    anchor: "b.png",
  });
});

test("the button ignores until every selected picture is ignored", () => {
  expect(ignoreAction([])).toBe(null);
  expect(ignoreAction([{ ignored: true }, {}])).toBe("ignore");
  expect(ignoreAction([{ ignored: true }, { ignored: true }])).toBe("unignore");
});
