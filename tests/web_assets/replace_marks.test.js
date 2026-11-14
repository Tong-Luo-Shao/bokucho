import { expect, test } from "bun:test";

import { fromFirstMark, markedPieces } from "../../src/bokucho/web/assets/replace_marks.js";

test("the marked occurrences and the text around them come back in order", () => {
  expect(markedPieces("光與光走了", [[0, 1], [2, 3]])).toEqual([
    { text: "光", marked: true },
    { text: "與", marked: false },
    { text: "光", marked: true },
    { text: "走了", marked: false },
  ]);
});

test("a line with no marks is one plain piece", () => {
  expect(markedPieces("沒有", [])).toEqual([{ text: "沒有", marked: false }]);
});

test("marks are counted in code points, as the backend counts them", () => {
  expect(markedPieces("𠮷野的光", [[3, 4]])).toEqual([
    { text: "𠮷野的", marked: false },
    { text: "光", marked: true },
  ]);
});

test("a long run-up to the first mark is cut down so the mark shows", () => {
  const pieces = markedPieces("很久很久以前有一道光", [[9, 10]]);
  expect(fromFirstMark(pieces, 4)).toEqual([
    { text: "…前有一道", marked: false },
    { text: "光", marked: true },
  ]);
});

test("a short run-up and a line with no marks stay whole", () => {
  const short = markedPieces("一道光", [[2, 3]]);
  expect(fromFirstMark(short, 4)).toEqual(short);
  const plain = markedPieces("很久很久以前沒有什麼", []);
  expect(fromFirstMark(plain, 4)).toEqual(plain);
});

test("where text was deleted the cut lands in the same place", () => {
  const after = markedPieces("很久很久以前有一道來了", [[9, 9]]);
  expect(fromFirstMark(after, 4)).toEqual([
    { text: "…前有一道", marked: false },
    { text: "來了", marked: false },
  ]);
});
