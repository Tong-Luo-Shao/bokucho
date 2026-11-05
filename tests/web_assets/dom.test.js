import { expect, test } from "bun:test";

import * as dom from "../../src/bokucho/web/assets/dom.js";

test("setting children omits absent optional nodes", () => {
  const parent = {
    children: [],
    replaceChildren(...children) {
      this.children = children;
    },
  };
  const first = {};
  const second = {};

  expect(typeof dom.setChildren).toBe("function");
  dom.setChildren?.(parent, [first, null, undefined, second]);

  expect(parent.children).toEqual([first, second]);
});
