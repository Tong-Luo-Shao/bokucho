import { expect, test } from "bun:test";

import * as modelSettings from "../../src/bokucho/web/assets/screens/model_settings.js";

test("editing an interface fills the API key field with its stored keys", async () => {
  expect(modelSettings.storedApiKeysText).toBeFunction();
  const bridge = {
    async provider_interface_keys(interfaceId) {
      return interfaceId === "selected"
        ? { api_keys: ["first-key", "second-key"] }
        : { api_keys: ["wrong-key"] };
    },
  };

  const text = await modelSettings.storedApiKeysText(bridge, {
    id: "selected",
    credential_count: 2,
  });

  expect(text).toBe("first-key\nsecond-key");
});

test("an interface without stored keys leaves the API key field blank", async () => {
  const bridge = {
    async provider_interface_keys() {
      throw new Error("empty interfaces must not request credentials");
    },
  };

  const text = await modelSettings.storedApiKeysText(bridge, {
    id: "empty",
    credential_count: 0,
  });

  expect(text).toBe("");
});

test("API key input fetches models once for the latest key after a pause", async () => {
  const loaded = [];
  let finish;
  const finished = new Promise((resolve) => {
    finish = resolve;
  });
  const input = modelSettings.createApiKeyModelLookup((key) => {
    loaded.push(key);
    finish();
  }, 0);

  input("old-key");
  input("new-key");
  await finished;
  await Bun.sleep(5);

  expect(loaded).toEqual(["new-key"]);
});

test("clearing API key input cancels pending model lookup", async () => {
  const loaded = [];
  const input = modelSettings.createApiKeyModelLookup((key) => loaded.push(key), 0);

  input("key-being-cleared");
  input("");
  await Bun.sleep(5);

  expect(loaded).toEqual([]);
});

test("stored keys stay with the address they were saved for", () => {
  const stored = "first-key\nsecond-key";
  const saved = "https://models.example/v1";

  expect(modelSettings.keysForAddress(stored, saved, "https://MODELS.example/v1/")).toBe(stored);
  expect(modelSettings.keysForAddress(stored, saved, "https://elsewhere.example/v1")).toBe("");
  expect(modelSettings.keysForAddress(stored, saved, "http://models.example/v1")).toBe("");
});
