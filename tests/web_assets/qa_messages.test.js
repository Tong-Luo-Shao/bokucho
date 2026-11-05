import { expect, test } from "bun:test";

import { QA_MESSAGES, issueText } from "../../src/bokucho/web/assets/qa_messages.js";
import { loadCatalog, t } from "../../src/bokucho/web/assets/i18n.js";

const lineCount = {
  code: "line_count",
  params: { source: 3, target: 2 },
  message: "line count changed: 3 -> 2",
};

test("a finding is told in the catalog's words with its values filled in", async () => {
  await loadCatalog(
    { get_catalog: async () => ({ QaIssue: { [QA_MESSAGES.line_count]: "行數改變：{source} → {target}" } }) },
    "zh_TW",
  );

  expect(issueText(t, lineCount)).toBe("行數改變：3 → 2");
});

test("a finding the catalog has no words for is told in the template's English", async () => {
  await loadCatalog({ get_catalog: async () => ({}) }, "en");

  expect(issueText(t, lineCount)).toBe("line count changed: 3 -> 2");
});

test("an empty value is spelled out in the interface's language", async () => {
  await loadCatalog(
    { get_catalog: async () => ({ QaIssue: { none: "無" } }) },
    "zh_TW",
  );

  const text = issueText(t, {
    code: "brackets_missing_or_extra",
    params: { source: null, target: "「」" },
    message: "brackets lost or added: none -> 「」",
  });

  expect(text).toBe("brackets lost or added: 無 -> 「」");
});

test("a code this build does not know falls back to the backend's message", async () => {
  await loadCatalog({ get_catalog: async () => ({}) }, "en");

  expect(
    issueText(t, { code: "from_a_newer_backend", params: {}, message: "something new" }),
  ).toBe("something new");
  expect(issueText(t, { code: "", message: "an issue with no code" })).toBe("an issue with no code");
});
