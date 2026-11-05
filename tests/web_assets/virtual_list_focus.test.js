import { afterEach, beforeEach, expect, test } from "bun:test";

import { createVirtualList } from "../../src/bokucho/web/assets/virtual_list.js";

class Row {
  constructor() {
    this.style = {};
    this.removed = false;
  }

  append() {}

  remove() {
    this.removed = true;
  }

  contains(node) {
    return node === this;
  }

  focus() {
    globalThis.document.activeElement = this;
  }
}

const savedDocument = globalThis.document;
const savedFrame = globalThis.requestAnimationFrame;
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

let viewport;
let list;
let rows;

beforeEach(() => {
  globalThis.document = {
    activeElement: null,
    createElement: () => new Row(),
    createDocumentFragment: () => new Row(),
  };
  globalThis.requestAnimationFrame = (callback) => queueMicrotask(callback);
  rows = new Map();
  const listeners = {};
  viewport = {
    scrollTop: 0,
    clientHeight: 480,
    style: {},
    replaceChildren() {},
    addEventListener(type, listener) {
      listeners[type] = listener;
    },
    scroll(to) {
      this.scrollTop = to;
      listeners.scroll();
    },
  };
  list = createVirtualList({
    viewport,
    rowHeight: 48,
    loadSlice: (offset, limit) => Array.from({ length: limit }, (_, index) => offset + index),
    renderRow: (_row, index) => {
      const node = new Row();
      rows.set(index, node);
      return node;
    },
  });
  list.setTotal(2000);
});

afterEach(() => {
  globalThis.document = savedDocument;
  globalThis.requestAnimationFrame = savedFrame;
});

test("a row far down the list takes the keyboard as soon as it is drawn", async () => {
  await settle();

  list.focusIndex(1500);
  await settle();

  expect(viewport.scrollTop).toBe(1500 * 48 + 48 - 480);
  expect(document.activeElement).toBe(rows.get(1500));
});

test("scrolling away from the focused row does not take its keyboard away", async () => {
  await settle();
  list.focusIndex(3);
  const focused = rows.get(3);

  viewport.scroll(1000 * 48);
  await settle();

  expect(focused.removed).toBe(false);
  expect(document.activeElement).toBe(focused);
});

test("a list redrawn after a change hands the keyboard to the row now at that place", async () => {
  await settle();
  list.focusIndex(2);
  const before = rows.get(2);

  list.setTotal(2000);
  await settle();

  expect(rows.get(2)).not.toBe(before);
  expect(document.activeElement).toBe(rows.get(2));
});
