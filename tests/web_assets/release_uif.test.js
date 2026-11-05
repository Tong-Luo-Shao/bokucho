import { expect, test } from "bun:test";

import { uifLabel } from "../../src/bokucho/web/assets/screens/release.js";

const t = (_context, text) => text;

test("the translator's own UIF is named over the one Bokucho carries", () => {
  expect(uifLabel({ path: "C:/uif/winmm.dll", bundled: true }, t)).toBe("C:/uif/winmm.dll");
  expect(uifLabel({ path: null, bundled: true }, t)).toBe("Bokucho's own");
  expect(uifLabel({ path: null, bundled: false }, t)).toBe("Not chosen");
});
