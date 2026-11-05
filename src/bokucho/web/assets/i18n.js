// i18n.js -- thin lookup over Api.get_catalog(locale); Python owns the catalog,
// this module only renders it. Falls back to the source string when the
// active catalog has no translation for it (matches Qt's tr() fallback).

let catalog = {};

export async function loadCatalog(bridge, locale) {
  const result = await bridge.get_catalog(locale);
  catalog = result && typeof result === "object" ? result : {};
  return catalog;
}

export function t(context, source, vars) {
  const text = catalog[context]?.[source] ?? source;
  if (!vars) return text;
  return Object.entries(vars).reduce(
    (acc, [key, value]) => acc.replaceAll(`{${key}}`, String(value)),
    text,
  );
}
