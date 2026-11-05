import { expect, test } from "bun:test";

import { listTarget } from "../../src/bokucho/web/assets/list_keys.js";

const TOTAL = 11827;
const PAGE_ROWS = 6;

test("an arrow moves one row and stops at either end of the queue", () => {
  expect(listTarget("ArrowDown", 49, TOTAL, PAGE_ROWS)).toBe(50);
  expect(listTarget("ArrowUp", 50, TOTAL, PAGE_ROWS)).toBe(49);
  expect(listTarget("ArrowUp", 0, TOTAL, PAGE_ROWS)).toBe(0);
  expect(listTarget("ArrowDown", TOTAL - 1, TOTAL, PAGE_ROWS)).toBe(TOTAL - 1);
});

test("Home and End go to the first and last row of the whole queue, not of one page", () => {
  expect(listTarget("Home", 5000, TOTAL, PAGE_ROWS)).toBe(0);
  expect(listTarget("End", 5000, TOTAL, PAGE_ROWS)).toBe(TOTAL - 1);
});

test("Page keys move a screenful and are held to the queue's ends", () => {
  expect(listTarget("PageDown", 10, TOTAL, PAGE_ROWS)).toBe(16);
  expect(listTarget("PageUp", 10, TOTAL, PAGE_ROWS)).toBe(4);
  expect(listTarget("PageUp", 2, TOTAL, PAGE_ROWS)).toBe(0);
  expect(listTarget("PageDown", TOTAL - 2, TOTAL, PAGE_ROWS)).toBe(TOTAL - 1);
});

test("a key that is not a movement key leaves the row alone", () => {
  expect(listTarget("a", 3, TOTAL, PAGE_ROWS)).toBeNull();
  expect(listTarget("Tab", 3, TOTAL, PAGE_ROWS)).toBeNull();
  expect(listTarget("Enter", 3, TOTAL, PAGE_ROWS)).toBeNull();
});
