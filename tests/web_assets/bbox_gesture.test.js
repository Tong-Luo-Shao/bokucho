import { expect, test } from "bun:test";

import {
  bboxCursor,
  bboxGestureAt,
  moveBox,
  resizeBox,
} from "../../src/bokucho/web/assets/canvas_editor.js";

const regions = [
  { box: [10, 10, 50, 30], text: "start" },
  { box: [10, 40, 50, 60], text: "load" },
];

const edges = (sides) => ({
  left: sides.includes("left"),
  top: sides.includes("top"),
  right: sides.includes("right"),
  bottom: sides.includes("bottom"),
});

test("a press on bare picture starts a new box", () => {
  expect(bboxGestureAt(regions, { x: 80, y: 80 }, 4)).toEqual({ kind: "create", index: -1 });
});

test("a press inside a box moves that box", () => {
  expect(bboxGestureAt(regions, { x: 20, y: 45 }, 4)).toEqual({ kind: "move", index: 1 });
});

test("a press on the far corner resizes the box it belongs to", () => {
  expect(bboxGestureAt(regions, { x: 49, y: 29 }, 4)).toEqual({
    kind: "resize",
    index: 0,
    edges: edges(["right", "bottom"]),
  });
});

test("every corner and side of a box is a grip", () => {
  const at = (x, y) => bboxGestureAt(regions, { x, y }, 4).edges;

  expect(at(11, 11)).toEqual(edges(["left", "top"]));
  expect(at(48, 11)).toEqual(edges(["right", "top"]));
  expect(at(11, 28)).toEqual(edges(["left", "bottom"]));
  expect(at(11, 20)).toEqual(edges(["left"]));
  expect(at(30, 11)).toEqual(edges(["top"]));
  expect(at(48, 20)).toEqual(edges(["right"]));
  expect(at(30, 28)).toEqual(edges(["bottom"]));
});

test("the chosen box can be caught just outside its edge, where its grip is drawn", () => {
  expect(bboxGestureAt(regions, { x: 52, y: 20 }, 4, 0)).toEqual({
    kind: "resize",
    index: 0,
    edges: edges(["right"]),
  });
});

test("just outside a box nobody chose is still bare picture", () => {
  expect(bboxGestureAt(regions, { x: 52, y: 20 }, 4)).toEqual({ kind: "create", index: -1 });
});

test("a box too small for a grip inside drags whole, and resizes from outside once chosen", () => {
  const small = [{ box: [10, 10, 16, 16], text: "" }];

  expect(bboxGestureAt(small, { x: 15, y: 15 }, 4, 0)).toEqual({ kind: "move", index: 0 });
  expect(bboxGestureAt(small, { x: 17, y: 17 }, 4, 0)).toEqual({
    kind: "resize",
    index: 0,
    edges: edges(["right", "bottom"]),
  });
});

test("the topmost box wins where two overlap", () => {
  const stacked = [
    { box: [0, 0, 40, 40], text: "under" },
    { box: [10, 10, 30, 30], text: "over" },
  ];

  expect(bboxGestureAt(stacked, { x: 20, y: 20 }, 4)).toEqual({ kind: "move", index: 1 });
});

test("a grip shows the cursor that matches the way it pulls", () => {
  expect(bboxCursor({ kind: "create" })).toBe("create");
  expect(bboxCursor({ kind: "move" })).toBe("move");
  expect(bboxCursor({ kind: "resize", edges: edges(["left", "top"]) })).toBe("nwse");
  expect(bboxCursor({ kind: "resize", edges: edges(["right", "top"]) })).toBe("nesw");
  expect(bboxCursor({ kind: "resize", edges: edges(["right"]) })).toBe("ew");
  expect(bboxCursor({ kind: "resize", edges: edges(["bottom"]) })).toBe("ns");
});

test("pulling an edge moves only that edge", () => {
  expect(resizeBox([10, 10, 50, 30], edges(["left"]), -4, 9, 100, 100)).toEqual([6, 10, 50, 30]);
  expect(resizeBox([10, 10, 50, 30], edges(["right", "bottom"]), 5, 5, 100, 100)).toEqual([
    10, 10, 55, 35,
  ]);
});

test("a resized box stays on the picture and never turns inside out", () => {
  expect(resizeBox([10, 10, 50, 30], edges(["right"]), 200, 0, 100, 100)).toEqual([
    10, 10, 100, 30,
  ]);
  expect(resizeBox([10, 10, 50, 30], edges(["left"]), 80, 0, 100, 100)).toEqual([49, 10, 50, 30]);
  expect(resizeBox([10, 10, 50, 30], edges(["top"]), 0, -50, 100, 100)).toEqual([10, 0, 50, 30]);
});

test("a moved box keeps its size and stops at the picture's edge", () => {
  expect(moveBox([10, 10, 50, 30], 5, -3, 100, 100)).toEqual([15, 7, 55, 27]);
  expect(moveBox([10, 10, 50, 30], 90, -90, 100, 100)).toEqual([60, 0, 100, 20]);
});
