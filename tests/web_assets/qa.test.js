import { expect, test } from "bun:test";

import { pageIssues } from "../../src/bokucho/web/assets/screens/qa.js";
import { findUnitIndex } from "../../src/bokucho/web/assets/screens/strings.js";

test("QA pages render at most 100 issues and report their visible range", () => {
  const issues = Array.from({ length: 251 }, (_, index) => index);

  const first = pageIssues(issues, 0);
  const second = pageIssues(issues, 1);
  const last = pageIssues(issues, 2);

  expect(first.issues).toHaveLength(100);
  expect(first.start).toBe(0);
  expect(second.issues[0]).toBe(100);
  expect(last.issues).toHaveLength(51);
  expect(last.start).toBe(200);
  expect(last.pageCount).toBe(3);
});

test("QA page index clamps after findings are removed", () => {
  expect(pageIssues([1, 2], 99)).toMatchObject({
    page: 0,
    pageCount: 1,
    start: 0,
    issues: [1, 2],
  });
});

test("QA navigation finds the matching file and unit across pages", async () => {
  const rows = Array.from({ length: 245 }, (_, index) => ({
    source_file: index === 222 ? "scene-2.lsb" : "scene-1.lsb",
    id: index === 222 ? "shared-id" : `shared-id-${index}`,
  }));
  rows[15] = { source_file: "scene-1.lsb", id: "shared-id" };
  const requested = [];

  const index = await findUnitIndex(
    async (offset, limit) => {
      requested.push(offset);
      return { total: rows.length, rows: rows.slice(offset, offset + limit) };
    },
    { source_file: "scene-2.lsb", id: "shared-id" },
    100,
  );

  expect(index).toBe(222);
  expect(requested).toEqual([0, 100, 200]);
});

test("QA navigation reports when a target is no longer in the project", async () => {
  const index = await findUnitIndex(
    async () => ({ total: 1, rows: [{ source_file: "other.lsb", id: "other" }] }),
    { source_file: "scene.lsb", id: "missing" },
  );

  expect(index).toBe(-1);
});
