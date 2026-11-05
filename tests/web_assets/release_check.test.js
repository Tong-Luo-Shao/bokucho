import { expect, test } from "bun:test";

import { checkFigures } from "../../src/bokucho/web/assets/screens/release.js";

test("the release check reports the queue's counts, one figure per status", () => {
  const counts = {
    total: 11827,
    status_counts: { untranslated: 27, mt: 11746, reviewed: 9, ignored: 45 },
    files: [],
    image_units: 2343,
  };

  expect(checkFigures(counts)).toEqual({
    total: 11827,
    untranslated: 27,
    pending: 11746,
    reviewed: 9,
  });
});

test("the release check has no figures before the queue's counts arrive", () => {
  expect(checkFigures(null)).toBeNull();
});
