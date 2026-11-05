import { expect, test } from "bun:test";

import { htmlLang } from "../../src/bokucho/web/assets/language_select.js";

test("project languages become valid BCP 47 tags for the lang attribute", () => {
  expect(htmlLang("ja")).toBe("ja");
  expect(htmlLang("zh-tw")).toBe("zh-TW");
  expect(htmlLang("zh-Hant")).toBe("zh-Hant");
  expect(htmlLang(" en ")).toBe("en");
});

test("a language that is not one is left unmarked", () => {
  expect(htmlLang("und")).toBeNull();
  expect(htmlLang("auto")).toBeNull();
  expect(htmlLang("")).toBeNull();
  expect(htmlLang(undefined)).toBeNull();
  expect(htmlLang("not a language")).toBeNull();
});
