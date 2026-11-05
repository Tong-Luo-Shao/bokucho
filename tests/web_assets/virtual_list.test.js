import { afterAll, expect, test } from "bun:test";

import { createVirtualList } from "../../src/bokucho/web/assets/virtual_list.js";

// Only what the list touches: a node it can style, fill and take apart.
function node() {
  return { style: {}, append() {}, remove() {} };
}

const hadDocument = "document" in globalThis;
globalThis.document ??= { createElement: node, createDocumentFragment: node };
afterAll(() => {
  if (!hadDocument) delete globalThis.document;
});

test("a list told its length again keeps the reader where they were", () => {
  const viewport = {
    scrollTop: 0,
    clientHeight: 480,
    style: {},
    replaceChildren() {},
    addEventListener() {},
  };
  const list = createVirtualList({
    viewport,
    rowHeight: 48,
    loadSlice: (offset, limit) => Array.from({ length: limit }, (_, index) => offset + index),
    renderRow: node,
  });
  list.setTotal(2000);
  viewport.scrollTop = 4800;

  // What the inventory does after a picture is ignored, or a job finishes.
  list.setTotal(2000);

  expect(viewport.scrollTop).toBe(4800);
});
