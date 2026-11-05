// settings.js -- appearance, editable project metadata and game source (an
// unpacked folder or a packed archive; a packed archive is unpacked into the
// project), and the settings that belong to the application rather than a run.

import { el, uniqueId } from "../dom.js";
import { createDirectoryPicker } from "../directory_picker.js";
import { createLanguageSelect, selectLanguage } from "../language_select.js";

// The font files name a face both ways the platform might know it, so only
// the names this webview draws by are offered: one it lacks falls back, and
// measures exactly as the fallback does.
const probe = document.createElement("canvas").getContext("2d");
const PROBE_TEXT = "AaBbWwMmIil1 永字八法 あア 한글";
const PROBE_BASES = ["monospace", "serif", "sans-serif"];
function probeWidth(family) {
  probe.font = `40px ${family}`;
  return probe.measureText(PROBE_TEXT).width;
}
function drawable(family) {
  return PROBE_BASES.some(
    (base) => probeWidth(`${JSON.stringify(family)}, ${base}`) !== probeWidth(base),
  );
}

export function mount(container, ctx) {
  const { t } = ctx;
  const appearance = appearancePanel(ctx);
  const details = new ProjectDetails(ctx);
  const application = applicationPanel(ctx);
  container.replaceChildren(el("div", { class: "settings-screen" }, [
    el("div", {
      class: "settings-head",
      role: "heading",
      "aria-level": "1",
      text: t("ProjectSettings", "Project settings"),
    }),
    el("div", { class: "settings-body" }, [
      el("div", { class: "settings-column" }, [appearance, details.element, application]),
    ]),
  ]));
  const refresh = () => details.refresh();
  refresh().catch((err) => ctx.showError(err));

  return { refresh, unmount() {} };
}

function panel(title, children) {
  return el("section", { class: "settings-panel" }, [
    el("div", { class: "settings-panel-head", text: title }),
    ...children,
  ]);
}

function row(label, control) {
  const labelId = uniqueId("settings-row-label");
  if (label && !control.hasAttribute("aria-label")) {
    control.setAttribute("aria-labelledby", labelId);
  }
  return el("div", { class: "settings-row" }, [
    el("span", { class: "settings-row-label", id: labelId, text: label }),
    control,
  ]);
}

function stacked(label, control) {
  return el("label", { class: "settings-field" }, [
    el("span", { class: "settings-field-label", text: label }),
    control,
  ]);
}

// A segmented control: the options are the whole set, so the chosen one is
// marked rather than the others hidden behind a menu.
function segmented(ctx, key, labels) {
  const buttons = Object.entries(labels).map(([value, label]) => {
    const button = el("button", { class: "settings-seg-option", type: "button", text: label });
    button.addEventListener("click", () => {
      ctx.setAppearance({ [key]: value });
      sync();
    });
    return [value, button];
  });
  function sync() {
    for (const [value, button] of buttons) {
      const chosen = String(ctx.appearance[key] === value);
      button.setAttribute("data-selected", chosen);
      button.setAttribute("aria-pressed", chosen);
    }
  }
  sync();
  return el("div", { class: "settings-seg", role: "group" }, buttons.map(([, button]) => button));
}

function appearancePanel(ctx) {
  const { t } = ctx;
  const fontSize = fontSizeSelect(ctx);
  const fontFamily = fontFamilySelect(ctx);
  return panel(t("ProjectSettings", "Appearance"), [
    row(t("ProjectSettings", "Theme"), segmented(ctx, "theme", {
      dark: t("ProjectSettings", "Dark"),
      light: t("ProjectSettings", "Light"),
    })),
    row(t("ProjectSettings", "Font size"), fontSize),
    row(t("ProjectSettings", "Font family"), fontFamily),
    row(t("ProjectSettings", "Row height"), segmented(ctx, "density", {
      compact: t("ProjectSettings", "Compact"),
      normal: t("ProjectSettings", "Normal"),
      roomy: t("ProjectSettings", "Roomy"),
    })),
    row(t("ProjectSettings", "Accent"), segmented(ctx, "accent", {
      clay: t("ProjectSettings", "Clay"),
      lime: t("ProjectSettings", "Lime"),
      amber: t("ProjectSettings", "Amber"),
      ice: t("ProjectSettings", "Ice"),
    })),
  ]);
}

function fontSizeSelect(ctx) {
  const fontSize = el("select", {
    id: "interface-font-size",
    "aria-label": ctx.t("ProjectSettings", "Font size"),
  }, Array.from({ length: 13 }, (_, index) => {
    const size = index + 12;
    return el("option", { value: String(size), text: `${size}px` });
  }));
  fontSize.value = String(ctx.appearance.fontSize);
  fontSize.addEventListener("change", () => {
    ctx.setAppearance({ fontSize: Number(fontSize.value) });
  });
  return fontSize;
}

function fontFamilySelect(ctx) {
  const fontFamily = el("select", {
    id: "interface-font-family",
    "aria-label": ctx.t("ProjectSettings", "Font family"),
  });
  function showFontFamilies(families) {
    const current = ctx.appearance.fontFamily;
    // One chosen before, or on another machine, stays listed so it shows.
    const names = current && !families.includes(current) ? [current, ...families] : families;
    fontFamily.replaceChildren(
      el("option", { value: "", text: ctx.t("ProjectSettings", "Default font") }),
      ...names.map((family) => el("option", { value: family, text: family })),
    );
    fontFamily.value = current;
  }
  showFontFamilies([]);
  fontFamily.addEventListener("change", () => {
    ctx.setAppearance({ fontFamily: fontFamily.value });
  });
  ctx.bridge
    .font_families()
    .then(({ families }) => showFontFamilies(families.filter(drawable)))
    .catch((err) => ctx.showError(err));
  return fontFamily;
}

class ProjectDetails {
  constructor(ctx) {
    const { t } = ctx;
    this.ctx = ctx;
    this.name = el("input", { type: "text" });
    this.sourceLang = createLanguageSelect(ctx, {
      id: "translation-source-language",
      label: t("ProjectSettings", "Source language"),
      detected: true,
    });
    this.targetLang = createLanguageSelect(ctx, {
      id: "translation-target-language",
      label: t("ProjectSettings", "Target language"),
    });
    this.engineInfo = el("p", { class: "muted settings-note" });
    this.error = el("p", { class: "severity-error settings-note" });
    this.gamePicker = createDirectoryPicker(ctx, {
      id: "settingsGamePath",
      label: t("ProjectSettings", "Game path"),
      placeholder: t("ProjectSettings", "Select the game executable"),
      browseText: t("ProjectSettings", "Browse"),
      files: true,
      onError: (err) => {
        this.error.textContent = err.message;
      },
    });
    this.element = this.panel();
  }

  panel() {
    const { t } = this.ctx;
    return panel(t("ProjectSettings", "Project details"), [
      el("div", { class: "settings-panel-body" }, [
        stacked(t("ProjectSettings", "Project name"), this.name),
        stacked(t("ProjectSettings", "Source language"), this.sourceLang),
        stacked(t("ProjectSettings", "Target language"), this.targetLang),
        stacked(t("ProjectSettings", "Game path"), this.gamePicker.element),
        this.engineInfo,
        this.error,
        el("div", { class: "settings-actions" }, [
          el("span", { class: "spacer" }),
          el("button", {
            class: "btn btn-sm btn-primary",
            type: "button",
            text: t("ProjectSettings", "Save project settings"),
            onClick: () => this.save(),
          }),
        ]),
      ]),
    ]);
  }

  async refresh() {
    const settings = await this.ctx.bridge.get_settings();
    Object.assign(this.ctx.appState.project, {
      source_lang: settings.source_lang,
      target_lang: settings.target_lang,
    });
    this.name.value = settings.name ?? "";
    selectLanguage(this.sourceLang, settings.source_lang);
    selectLanguage(this.targetLang, settings.target_lang);
    this.engineInfo.textContent = this.ctx.t("ProjectSettings", "Game engine") + `: ${settings.engine?.type ?? "--"}`;
    this.gamePicker.input.value = settings.engine?.game_path ?? "";
  }

  async save() {
    try {
      await this.ctx.bridge.save_project_settings(
        this.name.value.trim(),
        this.sourceLang.value.trim(),
        this.targetLang.value.trim(),
        this.gamePicker.input.value.trim(),
      );
      this.error.textContent = "";
      await this.refresh();
    } catch (err) {
      this.error.textContent = err.message;
    }
  }
}

function localeSelect(ctx) {
  const select = el("select", { id: "interface-locale" }, [
    el("option", { value: "zh_TW", text: "繁體中文", lang: "zh-Hant" }),
    el("option", { value: "en", text: "English", lang: "en" }),
  ]);
  select.value = ctx.locale();
  select.addEventListener("change", () => {
    ctx
      .setLocale(select.value)
      // The screen is rebuilt in the new language; the reader stays on the control they used.
      .then(() => document.getElementById("interface-locale")?.focus())
      .catch((err) => ctx.showError(err));
  });
  return select;
}

// The rail carries the eight screens the work runs through; the prompt
// library and the OCR model are settings, and this is where settings are.
function applicationPanel(ctx) {
  const { t } = ctx;
  const link = (text, route) =>
    el("button", { class: "btn btn-sm", type: "button", text, onClick: () => ctx.navigate(route) });
  return panel(t("ProjectSettings", "Application"), [
    row(t("ProjectSettings", "Interface language"), localeSelect(ctx)),
    row("", el("div", { class: "settings-seg" }, [
      link(t("ProjectSettings", "Prompt library"), "prompts"),
      link(t("ProjectSettings", "OCR model"), "ocr-settings"),
    ])),
  ]);
}
