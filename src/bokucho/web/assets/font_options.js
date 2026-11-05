// font_options.js -- the faces a typesetting font field offers, shared by the
// images screen and the image editor's text layer.

import { el } from "./dom.js";

// How a project names a font Bokucho ships (bokucho.appdirs.BUNDLED_FONT_PREFIX).
const BUNDLED_PREFIX = "bundled:";

// The faces after a field's own default: the chosen one first when the list
// does not hold it, then the ones Bokucho ships and the machine's, each under its
// heading. `installed` is null while it is still being read.
export function listedFontOptions(chosen, installed, t) {
  const listed = installed ?? [];
  const options = [];
  if (chosen && !listed.some((entry) => entry.path === chosen.path)) {
    options.push({ value: chosen.path, label: chosen.name ?? chosen.path });
  }
  for (const entry of listed) {
    options.push({
      value: entry.path,
      label: entry.name,
      group: entry.path.startsWith(BUNDLED_PREFIX)
        ? t("Fonts", "Shipped with Bokucho")
        : t("Fonts", "On this computer"),
    });
  }
  return options;
}

// `options` as <option>s, each that has a group gathered under its <optgroup>.
export function fontOptionElements(options) {
  const nodes = [];
  let group = null;
  for (const option of options) {
    const node = el("option", { value: option.value, text: option.label });
    if (!option.group) {
      nodes.push(node);
      group = null;
      continue;
    }
    if (group?.label !== option.group) {
      group = el("optgroup", { label: option.group });
      nodes.push(group);
    }
    group.append(node);
  }
  return nodes;
}
