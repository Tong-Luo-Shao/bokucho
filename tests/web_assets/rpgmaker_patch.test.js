import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const template = readFileSync(
  new URL("../../src/bokucho/resources/patches/rpgmaker.js", import.meta.url), "utf8",
);

function plugin(translations, pictures = {}, extra = {}) {
  const seen = [];
  const context = {
    ...extra,
    DataManager: {
      loadDataFile(name, source) { seen.push([name, source]); },
      onLoad(object) { seen.push(JSON.parse(JSON.stringify(object))); },
    },
    ImageManager: { loadBitmap(...args) { return args; } },
  };
  context.window = context;
  const values = {
    __BOKUCHO_TRANSLATIONS__: translations,
    __BOKUCHO_PICTURES__: pictures,
  };
  runInNewContext(template.replace(/__BOKUCHO_(?:TRANSLATIONS|PICTURES)__/g,
    (token) => JSON.stringify(values[token])), context);
  return { context, seen };
}

test("database and map text is mounted before the original onLoad handler", () => {
  const { context, seen } = plugin({
    "Actors.json": [[{ path: [1, "name"], source: "Hero", target: "勇者" }]],
    "Map001.json": [[{ path: ["displayName"], source: "Town", target: "村莊" }]],
  });
  context.DataManager.loadDataFile("$dataActors", "Actors.json");
  context.$dataActors = [null, { name: "Hero", note: "script data" }];
  context.DataManager.onLoad(context.$dataActors);
  expect(seen[1]).toEqual([null, { name: "勇者", note: "script data" }]);
  context.DataManager.loadDataFile("$dataMap", "Map001.json");
  context.$dataMap = { displayName: "Town" };
  context.DataManager.onLoad(context.$dataMap);
  expect(context.$dataMap.displayName).toBe("村莊");
});

test("a changed line keeps its whole message and unrelated data unchanged", () => {
  const { context } = plugin({ "Map001.json": [[
    { path: ["lines", 0], source: "first", target: "第一行" },
    { path: ["lines", 1], source: "second", target: "第二行" },
  ]] });
  context.DataManager.loadDataFile("$dataMap", "Map001.json");
  context.$dataMap = { lines: ["first", "other version"], id: 3 };
  context.DataManager.onLoad(context.$dataMap);
  expect(context.$dataMap).toEqual({ lines: ["first", "other version"], id: 3 });
});

test("translated pictures load beside originals through the existing image loader", () => {
  const { context } = plugin({}, { "img/pictures/sign": true });
  expect(context.ImageManager.loadBitmap("img/pictures/", "sign", 0, true)).toEqual(
    ["bokucho/img/pictures/", "sign", 0, true],
  );
  expect(context.ImageManager.loadBitmap("img/pictures/", "other")).toEqual(
    ["img/pictures/", "other"],
  );
});

test("MV keeps its unencrypted system-image exception after mounting", () => {
  const { context } = plugin({}, { "img/system/Window": true }, {
    Decrypter: { checkImgIgnore(url) { return url === "img/system/Window.png"; } },
  });
  expect(context.Decrypter.checkImgIgnore("bokucho/img/system/Window.png")).toBe(true);
  expect(context.Decrypter.checkImgIgnore("bokucho/img/pictures/sign.png")).toBe(false);
  expect(context.Decrypter.checkImgIgnore("img/system/Window.png")).toBe(true);
});
