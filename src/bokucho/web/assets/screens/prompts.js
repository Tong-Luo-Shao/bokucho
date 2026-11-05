// prompts.js -- the library of base prompts and the project's selection from
// it. Built-ins are served read-only; a user preset is editable and can be
// removed. The selection lives with the project because it shows in the output.

import { el, labelControl, setChildren, uniqueId } from "../dom.js";
import { confirmDialog } from "../confirm.js";
import { describeDialog, hideDialog, showDialog, trapFocus } from "../dialog.js";

export function mount(container, ctx) {
  const library = new PromptLibrary(ctx);
  container.replaceChildren(library.element);
  const refresh = () => library.refresh();
  refresh().catch((err) => ctx.showError(err));

  return {
    refresh,
    unmount() {},
  };
}

function field(label, input, hint = null) {
  const children = [labelControl(el("label", { text: label }), input), input];
  if (hint) children.push(el("span", { class: "field-hint", text: hint }));
  return el("div", { class: "field-row" }, children);
}

function pageHeader(t) {
  return el("div", { class: "page-header" }, [
    el("div", { class: "heading" }, [
      el("h1", { class: "page-title", text: t("Prompts", "Prompts") }),
      el("p", {
        class: "page-description",
        text: t(
          "Prompts",
          "The instruction sent with every batch. Built-in wordings are read-only.",
        ),
      }),
    ]),
  ]);
}

class PromptLibrary {
  constructor(ctx) {
    this.ctx = ctx;
    this.presets = [];
    this.selectedId = "";
    this.list = el("div", { class: "preset-list", role: "list" });
    this.editor = new PromptEditor(ctx, (presets) => {
      this.presets = presets;
      this.render();
    });
    this.element = el("div", {}, [pageHeader(ctx.t), this.libraryCard(), this.editor.element]);
  }

  libraryCard() {
    const { t } = this.ctx;
    return el("div", { class: "card" }, [
      el("h2", { class: "card-title", text: t("Prompts", "Library") }),
      this.list,
      el("div", { class: "button-bar" }, [
        el("div", { class: "spacer" }),
        el("button", {
          class: "btn btn-primary",
          type: "button",
          text: t("Prompts", "New prompt"),
          onclick: () => this.editor.open(null, this.selectedPreset()),
        }),
      ]),
    ]);
  }

  // A new prompt starts from the selected wording rather than from nothing:
  // the placeholders are easy to get wrong and tedious to retype.
  selectedPreset() {
    return this.presets.find((item) => item.id === this.selectedId) ?? this.presets[0];
  }

  render() {
    setChildren(this.list, this.presets.map((preset) => this.row(preset)));
  }

  row(preset) {
    const { t } = this.ctx;
    const active = preset.id === this.selectedId;
    return el("div", { class: `preset-row${active ? " is-active" : ""}`, role: "listitem" }, [
      el("div", { class: "preset-identity" }, [
        el("strong", { text: preset.name }),
        preset.builtin
          ? el("span", { class: "preset-badge", text: t("Prompts", "Built-in") })
          : null,
        active
          ? el("span", { class: "preset-badge is-active", text: t("Prompts", "In use") })
          : null,
      ]),
      el("p", { class: "muted preset-excerpt", text: preset.system }),
      el("div", { class: "button-bar" }, [el("div", { class: "spacer" }), ...this.rowActions(preset, active)]),
    ]);
  }

  rowActions(preset, active) {
    const { t } = this.ctx;
    const button = (text, onclick) => el("button", { class: "btn btn-sm", type: "button", text, onclick });
    const actions = [];
    if (!active) actions.push(button(t("Prompts", "Use"), () => this.select(preset.id)));
    if (!preset.builtin) {
      actions.push(
        button(t("Prompts", "Edit"), () => this.editor.open(preset)),
        button(t("Prompts", "Delete"), () => this.remove(preset)),
      );
    }
    return actions;
  }

  async select(presetId) {
    try {
      const result = await this.ctx.bridge.select_prompt_preset(presetId);
      this.selectedId = result.selected_id;
      this.render();
    } catch (error) {
      this.ctx.showError(error);
    }
  }

  async remove(preset) {
    const { t } = this.ctx;
    const confirmed = await confirmDialog(this.ctx, {
      message: t("Prompts", "Delete the prompt {name}?", { name: preset.name }),
      confirmText: t("Prompts", "Delete"),
    });
    if (!confirmed) return;
    try {
      const result = await this.ctx.bridge.delete_prompt_preset(preset.id);
      this.presets = result.presets;
      if (this.selectedId === preset.id) this.selectedId = "";
      this.render();
    } catch (error) {
      this.ctx.showError(error);
    }
  }

  async refresh() {
    const result = await this.ctx.bridge.prompt_presets();
    this.presets = result.presets;
    this.selectedId = result.selected_id;
    this.render();
  }
}

class PromptEditor {
  constructor(ctx, onSaved) {
    this.ctx = ctx;
    this.onSaved = onSaved;
    this.editingId = null;
    this.nameInput = el("input", { type: "text" });
    this.systemInput = el("textarea", { rows: "12", class: "mono" });
    this.userInput = el("textarea", { rows: "3", class: "mono" });
    this.error = el("p", { class: "severity-error", hidden: true });
    this.title = el("h2", { class: "modal-title", id: uniqueId("prompt-editor-title") });
    this.element = el("div", { class: "modal-overlay", hidden: true }, [this.panel()]);
    describeDialog(this.element, { labelledBy: this.title.id });
    trapFocus(this.element, { onEscape: () => this.close() });
    this.element.addEventListener("click", (event) => {
      if (event.target === this.element) this.close();
    });
  }

  panel() {
    const { t } = this.ctx;
    return el("div", { class: "modal-panel prompt-editor" }, [
      this.title,
      field(t("Prompts", "Name"), this.nameInput),
      field(
        t("Prompts", "System prompt"),
        this.systemInput,
        t("Prompts", "Placeholders: {Orig_Lang} {Target_Lang} {glossary_section} {n}. Language tags use BCP 47; {source_lang} and {target_lang} also work."),
      ),
      field(
        t("Prompts", "User prompt"),
        this.userInput,
        t("Prompts", "Must contain {entries_json}."),
      ),
      this.error,
      el("div", { class: "modal-actions" }, [
        el("button", {
          class: "btn",
          type: "button",
          text: t("Common", "Cancel"),
          onclick: () => this.close(),
        }),
        el("button", {
          class: "btn btn-primary",
          type: "button",
          text: t("Common", "Save"),
          onclick: () => this.save(),
        }),
      ]),
    ]);
  }

  open(preset, template = preset) {
    const { t } = this.ctx;
    this.editingId = preset?.id ?? null;
    this.title.textContent = preset
      ? t("Prompts", "Edit prompt")
      : t("Prompts", "New prompt");
    this.nameInput.value = preset?.name ?? "";
    this.systemInput.value = template?.system ?? "";
    this.userInput.value = template?.user ?? "{entries_json}";
    this.error.hidden = true;
    showDialog(this.element, this.nameInput);
  }

  close() {
    hideDialog(this.element);
  }

  showError(message) {
    this.error.textContent = message;
    this.error.hidden = false;
  }

  async save() {
    const name = this.nameInput.value.trim();
    if (!name) {
      this.showError(this.ctx.t("Prompts", "A prompt needs a name."));
      return;
    }
    try {
      const result = await this.ctx.bridge.save_prompt_preset({
        id: this.editingId ?? `user:${name.toLowerCase().replace(/\s+/g, "-")}`,
        name,
        system: this.systemInput.value,
        user: this.userInput.value,
      });
      this.close();
      this.onSaved(result.presets);
    } catch (error) {
      this.showError(error.message);
    }
  }
}
