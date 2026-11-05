// glossary.js -- terminology workstation: a searchable grid whose target and
// note cells are edited in place, plus a draft row for adding a term and the
// build-from-script pass. Occurrence counts and origin ride each row.

import { el, setChildren } from "../dom.js";
import { confirmDialog } from "../confirm.js";
import { htmlLang } from "../language_select.js";

// The pass writes as it goes, so a run that failed partway still changed
// the table.
const JOB_ENDED = new Set(["succeeded", "failed", "cancelled"]);

// An edit persists to the file the entry already lives in; a global-only or
// shadowed entry is a global one, everything else is the project's.
function scopeOf(origin) {
  return origin === "global" || origin === "shadowed" ? "global" : "project";
}

export function mount(container, ctx) {
  const table = new GlossaryTable(ctx);
  container.replaceChildren(table.element);
  const refresh = () => table.refresh();
  const unsubscribe = ctx.onEvent((event) => {
    if (event.job === "glossary" && JOB_ENDED.has(event.type)) {
      refresh().catch((err) => ctx.showError(err));
    }
  });

  refresh().catch((err) => ctx.showError(err));

  return {
    refresh,
    unmount() {
      unsubscribe();
    },
  };
}

// Names each column, with what it does on hover. It scrolls with the rows,
// pinned at the top, so it lines up with them under the scrollbar gutter.
function columnHeader(t) {
  const headerCell = (cls, label, help) =>
    el("span", { class: `glossary-head-cell ${cls}`, text: label, title: help });
  return el("div", { class: "glossary-row glossary-head", role: "presentation" }, [
    headerCell("", t("Glossary", "Source"), t("Glossary", "The term as it appears in the game's text.")),
    headerCell(
      "",
      t("Glossary", "Target"),
      t("Glossary", "How the term is translated. Click to edit."),
    ),
    headerCell(
      "",
      t("Glossary", "Note"),
      t("Glossary", "A note for you. The translator works from Source and Target only."),
    ),
    el("div", { class: "glossary-right" }, [
      headerCell(
        "glossary-origin-col",
        t("Glossary", "Scope"),
        t(
          "Glossary",
          "Project terms belong to this project; global terms are shared by every project. A struck-out global term is overridden by a project term with the same source.",
        ),
      ),
      headerCell(
        "glossary-hits",
        t("Glossary", "Count"),
        t("Glossary", "How many times the term appears in this project's text."),
      ),
      el("span", { class: "glossary-delete-col" }),
    ]),
  ]);
}

function originLabel(t, origin) {
  return t("Glossary", origin === "global" ? "global" : origin === "shadowed" ? "shadowed" : "project");
}

function cellInput(value, aria, onCommit, lang = null) {
  const input = el("input", {
    type: "text",
    class: "glossary-cell-input",
    value: value ?? "",
    lang,
    "aria-label": aria,
  });
  input.addEventListener("change", () => onCommit(input.value.trim()));
  return input;
}

class GlossaryTable {
  constructor(ctx) {
    const { t } = ctx;
    this.ctx = ctx;
    this.entries = [];
    this.search = el("input", {
      type: "text",
      class: "glossary-search",
      placeholder: t("Glossary", "Search source or target"),
      "aria-label": t("Glossary", "Search source or target"),
    });
    this.search.addEventListener("input", () => this.renderList());
    this.rows = el("div", { class: "glossary-viewport", role: "list" });
    this.header = columnHeader(t);
    this.countLabel = el("span", { class: "glossary-count" });
    this.element = el("div", { class: "glossary-screen" }, [this.toolbar(), this.rows, this.statusBar()]);
  }

  toolbar() {
    const { t } = this.ctx;
    return el("div", { class: "glossary-toolbar" }, [
      this.search,
      el("span", { class: "spacer" }),
      el("button", {
        class: "btn",
        type: "button",
        text: t("Glossary", "Read the script for names"),
        title: t(
          "Glossary",
          "Settles one rendering per proper noun before translating. Terms already in the table keep their rendering.",
        ),
        onClick: () => this.ctx.startJob("glossary"),
      }),
      el("button", {
        class: "btn btn-primary",
        type: "button",
        text: t("Glossary", "Add term"),
        onClick: () => this.addDraftRow(),
      }),
    ]);
  }

  statusBar() {
    return el("footer", { class: "glossary-status" }, [
      this.countLabel,
      el("span", { class: "spacer" }),
      el("span", {
        class: "glossary-hint",
        text: this.ctx.t("Glossary", "Terms are underlined in the source column of the string list."),
      }),
    ]);
  }

  projectLang(key) {
    return htmlLang(this.ctx.appState.project?.[key]);
  }

  row(entry) {
    const { t } = this.ctx;
    const targetCell = cellInput(entry.target, t("Glossary", "Target"), (value) => {
      entry.target = value;
      this.persist(entry).catch((err) => this.ctx.showError(err));
    }, this.projectLang("target_lang"));
    const noteCell = cellInput(entry.note ?? "", t("Glossary", "Note"), (value) => {
      entry.note = value || null;
      this.persist(entry).catch((err) => this.ctx.showError(err));
    });
    return el("div", { class: "glossary-row", role: "listitem" }, [
      el("span", {
        class: "glossary-source",
        lang: this.projectLang("source_lang"),
        text: entry.source,
        title: entry.source,
      }),
      targetCell,
      noteCell,
      el("div", { class: "glossary-right" }, [
        el("span", {
          class: `glossary-origin origin-${entry.origin}`,
          text: originLabel(t, entry.origin),
        }),
        el("span", {
          class: "glossary-hits",
          text: String(entry.occurrences),
          title: t("Glossary", "Occurrences"),
        }),
        this.deleteButton(entry),
      ]),
    ]);
  }

  deleteButton(entry) {
    const { t } = this.ctx;
    const button = el("button", {
      class: "glossary-delete",
      type: "button",
      text: "×",
      title: t("Glossary", "Delete"),
      "aria-label": t("Glossary", "Delete"),
    });
    button.addEventListener("click", () => this.deleteEntry(entry, button));
    return button;
  }

  async deleteEntry(entry, button) {
    const { t } = this.ctx;
    const confirmed = await confirmDialog(this.ctx, {
      message: t("Glossary", "Delete the term {source}?", { source: entry.source }),
      confirmText: t("Glossary", "Delete"),
    });
    if (!confirmed) return;
    const at = [...this.rows.querySelectorAll(".glossary-delete")].indexOf(button);
    try {
      await this.ctx.bridge.delete_glossary_entry(entry.source, scopeOf(entry.origin));
      await this.refresh();
      const remaining = this.rows.querySelectorAll(".glossary-delete");
      (remaining[Math.min(at, remaining.length - 1)] ?? this.search).focus();
    } catch (err) {
      this.ctx.showError(err);
    }
  }

  async persist(entry) {
    await this.ctx.bridge.save_glossary_entry(
      entry.source,
      entry.target,
      entry.note,
      scopeOf(entry.origin),
    );
  }

  shownEntries() {
    const needle = this.search.value.trim().toLocaleLowerCase();
    return needle
      ? this.entries.filter(
          (e) =>
            e.source.toLocaleLowerCase().includes(needle) ||
            e.target.toLocaleLowerCase().includes(needle),
        )
      : this.entries;
  }

  renderList() {
    const shown = this.shownEntries();
    setChildren(this.rows, [this.header, ...shown.map((entry) => this.row(entry))]);
    this.countLabel.textContent = this.ctx.t("Glossary", "{count} terms", { count: shown.length });
  }

  addDraftRow() {
    const { t } = this.ctx;
    const draftInput = (key, label) => el("input", {
      type: "text",
      class: "glossary-cell-input",
      lang: this.projectLang(key),
      placeholder: label,
      "aria-label": label,
    });
    const sourceInput = draftInput("source_lang", t("Glossary", "Source"));
    const targetInput = draftInput("target_lang", t("Glossary", "Target"));
    const globalToggle = el("input", { type: "checkbox", "aria-label": t("Glossary", "Global") });
    const confirm = el("button", {
      class: "btn btn-sm btn-primary",
      type: "button",
      text: t("Glossary", "Add or update"),
      onClick: () => this.saveDraft(sourceInput, targetInput, globalToggle.checked),
    });
    const draft = el("div", { class: "glossary-row is-draft", role: "listitem" }, [
      sourceInput,
      targetInput,
      el("label", { class: "glossary-draft-global" }, [globalToggle, ` ${t("Glossary", "Global")}`]),
      el("div", { class: "glossary-right" }, [confirm]),
    ]);
    if (!this.header.isConnected) this.rows.prepend(this.header);
    this.header.after(draft);
    sourceInput.focus();
  }

  async saveDraft(sourceInput, targetInput, global) {
    const source = sourceInput.value.trim();
    const target = targetInput.value.trim();
    if (!source || !target) {
      sourceInput.focus();
      return;
    }
    try {
      await this.ctx.bridge.save_glossary_entry(source, target, null, global ? "global" : "project");
      await this.refresh();
    } catch (err) {
      this.ctx.showError(err);
    }
  }

  async refresh() {
    this.entries = await this.ctx.bridge.glossary_entries();
    this.renderList();
  }
}
