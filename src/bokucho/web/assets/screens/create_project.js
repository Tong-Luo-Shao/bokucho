// create_project.js -- validated project-creation dialog (destination, name,
// languages, engine, optional game path that detects its own engine).

import { el, labelControl, uniqueId } from "../dom.js";
import { createDirectoryPicker } from "../directory_picker.js";
import { describeDialog, returnFocus, trapFocus } from "../dialog.js";
import { createLanguageSelect } from "../language_select.js";

const ENGINE_LABELS = {
  livemaker: "LiveMaker / LiveNovel",
  renpy: "Ren'Py",
  "rpgmaker-mv": "RPG Maker MV",
  "rpgmaker-mz": "RPG Maker MZ",
  "rpgmaker-xp": "RPG Maker XP",
  "rpgmaker-vx": "RPG Maker VX",
  "rpgmaker-vx-ace": "RPG Maker VX Ace",
};

export function engineOptions(engines) {
  return engines.map((engine) => ({
    value: engine,
    engine,
    label: ENGINE_LABELS[engine] ?? engine,
  }));
}

export function engineValueForDetection(found) {
  if (found.engine !== "rpgmaker") return found.engine;
  const evidence = found.evidence.map((path) => path.toLowerCase());
  if (evidence.some((path) => path.endsWith(".rmmzproject") || path === "data/system.json")) {
    return "rpgmaker-mz";
  }
  if (evidence.some((path) => path.endsWith(".rgss3a") || path.endsWith("system.rvdata2"))) {
    return "rpgmaker-vx-ace";
  }
  return "rpgmaker-mv";
}

export function gameNameFromPath(path) {
  const parts = path.trim().replace(/[\\/]+$/, "").split(/[\\/]/).filter(Boolean);
  const selected = parts.at(-1) ?? "";
  return /\.(?:exe|dat|rgssad|rgss2a|rgss3a|bat|cmd)$/i.test(selected)
    ? parts.at(-2) ?? selected.replace(/\.[^.]+$/, "")
    : selected;
}

export function openCreateProjectDialog(ctx, onCreated) {
  const { t } = ctx;
  const fields = createProjectFields(ctx);
  const cancelButton = el("button", { class: "btn", type: "button", text: t("CreateProject", "Cancel") });
  const submitButton = el("button", { class: "btn btn-primary", type: "button", text: t("CreateProject", "Create project") });
  const titleId = uniqueId("create-project-title");
  const overlay = el("div", { class: "modal-overlay" }, [
    createProjectPanel(t, fields, titleId, [cancelButton, submitButton]),
  ]);
  const opener = document.activeElement;
  describeDialog(overlay, { labelledBy: titleId });
  document.body.append(overlay);
  fields.gamePath.focus();

  function close() {
    overlay.remove();
    returnFocus(opener);
  }

  trapFocus(overlay, { onEscape: close });
  cancelButton.addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  watchGamePath(ctx, fields);
  submitButton.addEventListener("click", () =>
    submitProject(ctx, fields, (created) => {
      close();
      onCreated(created);
    }),
  );
}

function createProjectFields(ctx) {
  const { t } = ctx;
  const error = el("p", { class: "severity-error" });
  const showError = (err) => {
    error.textContent = err.message;
  };
  const destinationPicker = createDirectoryPicker(ctx, {
    id: "projectDestination",
    label: t("CreateProject", "Destination"),
    placeholder: t("CreateProject", "Select the project destination folder"),
    browseText: t("CreateProject", "Browse"),
    onError: showError,
  });
  const name = el("input", { type: "text" });
  const sourceLang = createLanguageSelect(ctx, {
    id: "create-project-source-language",
    label: t("CreateProject", "Source language"),
    detected: true,
    value: "ja",
  });
  const targetLang = createLanguageSelect(ctx, {
    id: "create-project-target-language",
    label: t("CreateProject", "Target language"),
    value: "zh-tw",
  });
  const engine = el("select");
  const gamePicker = createDirectoryPicker(ctx, {
    id: "gamePath",
    label: t("CreateProject", "Game path"),
    placeholder: t("CreateProject", "Select the game executable"),
    browseText: t("CreateProject", "Browse"),
    files: true,
    onError: showError,
  });
  ctx.bridge
    .list_engines()
    .then((engines) => {
      engine.append(...engineOptions(engines).map((option) =>
        el("option", { value: option.value, text: option.label, "data-engine": option.engine })));
    })
    .catch(showError);
  return {
    error,
    showError,
    destinationPicker,
    destination: destinationPicker.input,
    name,
    sourceLang,
    targetLang,
    engine,
    gamePicker,
    gamePath: gamePicker.input,
    detected: el("p", { class: "muted" }),
  };
}

function createProjectPanel(t, fields, titleId, actions) {
  const field = (label, input) =>
    el("div", { class: "field-row" }, [labelControl(el("label", { text: label }), input), input]);
  return el("div", { class: "modal-panel" }, [
    el("h2", { class: "card-title", id: titleId, text: t("CreateProject", "Create Bokucho project") }),
    field(t("CreateProject", "Game path"), el("div", {}, [fields.gamePicker.element, fields.detected])),
    field(t("CreateProject", "Project name"), fields.name),
    field(t("CreateProject", "Destination"), fields.destinationPicker.element),
    field(
      `${t("CreateProject", "Source language")} → ${t("CreateProject", "Target language")}`,
      el("div", { class: "create-project-language-pair" }, [
        fields.sourceLang,
        el("span", { class: "create-project-language-arrow", text: "→", "aria-hidden": "true" }),
        fields.targetLang,
      ]),
    ),
    field(t("CreateProject", "Engine"), fields.engine),
    fields.error,
    el("div", { class: "modal-actions" }, actions),
  ]);
}

// Detection runs off the field itself: picking a game is the whole gesture,
// and typing a path still fires `change` on blur.
function watchGamePath(ctx, fields) {
  let suggestedName = "";
  fields.gamePath.addEventListener("change", () => {
    const selected = fields.gamePath.value.trim();
    fields.detected.textContent = "";
    if (!selected) return;
    const gameName = gameNameFromPath(selected);
    if (!fields.name.value.trim() || fields.name.value === suggestedName) {
      fields.name.value = gameName;
      suggestedName = gameName;
    }
    detectEngine(ctx, fields, selected);
  });
}

async function detectEngine(ctx, fields, selected) {
  const { t } = ctx;
  try {
    const result = await ctx.bridge.detect_engine(selected);
    const supported = result.results.filter((found) => found.supported);
    if (!supported.length) {
      fields.detected.textContent = t(
        "CreateProject",
        "No supported engine found there — set the engine manually.",
      );
      return;
    }
    fields.engine.value = engineValueForDetection(supported[0]);
    fields.detected.textContent = t("CreateProject", "Detected {engine} ({confidence})", {
      engine: fields.engine.selectedOptions[0]?.textContent ?? supported[0].label,
      confidence: supported[0].confidence.toFixed(2),
    });
    fields.error.textContent = "";
  } catch (err) {
    fields.showError(err);
  }
}

async function submitProject(ctx, fields, finish) {
  const values = {
    destination: fields.destination.value.trim(),
    name: fields.name.value.trim(),
    source_lang: fields.sourceLang.value.trim(),
    target_lang: fields.targetLang.value.trim(),
    engine: fields.engine.selectedOptions[0]?.dataset.engine ?? fields.engine.value,
    game: fields.gamePath.value.trim() || null,
  };
  if (!values.destination || !values.name || !values.source_lang || !values.target_lang) {
    fields.error.textContent = ctx.t(
      "CreateProject",
      "Destination, name, languages, and engine are required.",
    );
    return;
  }
  try {
    const created = await ctx.bridge.create_project(
      values.name,
      values.engine || null,
      values.destination,
      values.source_lang,
      values.target_lang,
      values.game,
    );
    finish(created);
  } catch (err) {
    fields.showError(err);
  }
}
