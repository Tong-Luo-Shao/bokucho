import { expect, test } from "bun:test";

import { createBoundedLog } from "../../src/bokucho/web/assets/bounded_log.js";

test("bounded job details retain only the newest lines", () => {
  const log = createBoundedLog(3);

  for (const line of ["one", "two", "three", "four", "five"]) log.append(line);

  expect(log.lines()).toEqual(["three", "four", "five"]);
  expect(log.dropped).toBe(2);
});

test("clearing job details resets retained and omitted lines", () => {
  const log = createBoundedLog(1);
  log.append("one");
  log.append("two");

  log.clear();

  expect(log.lines()).toEqual([]);
  expect(log.dropped).toBe(0);
});

test("progress updates replace one live line instead of flooding the log", () => {
  const log = createBoundedLog(3);
  log.append("START images-ocr");

  log.upsert("images-ocr:runtime-download", "0.10 / 1.00 MB");
  log.upsert("images-ocr:runtime-download", "0.50 / 1.00 MB");

  expect(log.lines()).toEqual(["START images-ocr", "0.50 / 1.00 MB"]);
  expect(log.dropped).toBe(0);
});
