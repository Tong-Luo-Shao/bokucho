import { expect, test } from "bun:test";

import * as overview from "../../src/bokucho/web/assets/screens/overview.js";
import * as strings from "../../src/bokucho/web/assets/screens/strings.js";

test("opening file progress carries its unit ID search to the strings screen", () => {
  const routes = [];
  const ctx = {
    appState: {},
    navigate: (route) => routes.push(route),
  };

  expect(typeof overview.openFileProgress).toBe("function");
  expect(typeof strings.takePendingStringSearch).toBe("function");

  overview.openFileProgress?.(ctx, "Actors");

  expect(routes).toEqual(["strings"]);
  expect(strings.takePendingStringSearch?.(ctx.appState)).toEqual({
    query: "Actors",
    scope: "id",
  });
  expect(ctx.appState.pendingStringSearch).toBeNull();
});
