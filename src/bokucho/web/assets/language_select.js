// Shared language choices for project settings and translation controls.

import { el } from "./dom.js";

const LANGUAGES = [
  "ja", "en", "zh-tw", "zh-cn", "ko",
  "fr", "de", "es", "pt", "it", "ru", "uk", "pl", "nl",
  "tr", "ar", "th", "vi", "id", "ms", "hi",
];

function languageLabel(tag) {
  try {
    const names = new Intl.DisplayNames([tag], { type: "language" });
    const nameTag = { "zh-tw": "zh-Hant", "zh-cn": "zh-Hans" }[tag.toLowerCase()] ?? tag;
    return `${names.of(nameTag)}(${tag})`;
  } catch {
    return tag;
  }
}

// The `lang` a project language marks its text with, so a screen reader speaks
// Japanese as Japanese. "und" is the detection choice: it names no language.
export function htmlLang(tag) {
  const value = String(tag ?? "").trim();
  if (!value || ["und", "auto"].includes(value.toLowerCase())) return null;
  try {
    return Intl.getCanonicalLocales(value)[0];
  } catch {
    return null;
  }
}

export function applyLang(node, tag) {
  const lang = htmlLang(tag);
  if (lang) node.lang = lang;
  else node.removeAttribute("lang");
}

export function selectLanguage(select, value) {
  if (value && !Array.from(select.options).some((option) => option.value === value)) {
    select.append(el("option", { value, text: languageLabel(value) }));
  }
  select.value = value ?? "";
}

export function createLanguageSelect(ctx, { id, label, detected = false, value, languages = LANGUAGES }) {
  const select = el("select", { id, "aria-label": label, title: label }, [
    detected ? el("option", {
      value: "und",
      text: ctx.t("ProjectSettings", "Detected language"),
    }) : null,
    ...languages.map((tag) => el("option", {
      value: tag,
      text: languageLabel(tag),
    })),
  ]);
  if (value !== undefined) selectLanguage(select, value);
  return select;
}
