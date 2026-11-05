// strings.js -- paged unit queue, focused editor, and bulk actions.
//
// The queue's glossary-underline decoration lives only in the editor's source
// panel: `unit_rows` (the paged list) does not carry `spans`, only
// `unit_detail` does, so that is the one place span-wrapped HTML is built.

import { el, clear, escapeHtml, setChildren } from "../dom.js";
import { confirmDialog } from "../confirm.js";
import { createImageEditorHost } from "../image_editor_modal.js";
import { applyLang, createLanguageSelect, htmlLang } from "../language_select.js";
import { listTarget } from "../list_keys.js";
import { checkLabel, issueText } from "../qa_messages.js";

const MUTATING_JOBS = new Set([
  "extract",
  "translate",
  "transform",
  "recheck",
  "fix-brackets",
  "fix-values",
]);
// Batches are saved as they land, so a run that failed or was cancelled
// halfway still changed the project on disk.
const JOB_ENDED = new Set(["succeeded", "failed", "cancelled"]);
const SEARCH_DEBOUNCE_MS = 300;
// A run saves a file every few seconds; redrawing the page for each is enough
// to watch it land without flooding the bridge.
const LIVE_REFRESH_MS = 1_000;

const STATUS_LABELS = {
  untranslated: "Untranslated",
  mt: "MT",
  reviewed: "Reviewed",
  ignored: "Ignored",
};
// "Ignored" last: it is the tab a translator opens to check something, not one
// they work out of.
const STATUS_TAB_ORDER = ["", "untranslated", "mt", "reviewed", "ignored"];
const PAGE_SIZES = [50, 100];
const PAGER_STEPS = [
  { glyph: "«", label: "First page", to: () => 0 },
  { glyph: "‹", label: "Previous page", to: (page) => page - 1 },
  { glyph: "›", label: "Next page", to: (page) => page + 1 },
  { glyph: "»", label: "Last page", to: (_page, pages) => pages - 1 },
];
// Reviewed is done, untranslated is empty, and everything in between is a
// draft someone still has to look at.
const STATUS_MARKS = {
  untranslated: "○",
  mt: "■",
  reviewed: "✓",
  ignored: "–",
};
const ORIGIN_LABELS = { memory: "Memory", glossary: "Glossary" };

export async function findUnitIndex(fetchRows, target, batchSize = 200) {
  let offset = 0;
  let total = Number.POSITIVE_INFINITY;

  while (offset < total) {
    const result = await fetchRows(offset, batchSize);
    total = result.total;
    const match = result.rows.findIndex(
      (row) => row.source_file === target.source_file && row.id === target.id,
    );
    if (match !== -1) return offset + match;
    if (result.rows.length === 0) break;
    offset += result.rows.length;
  }
  return -1;
}

export function takePendingStringSearch(appState) {
  const pending = appState.pendingStringSearch ?? null;
  appState.pendingStringSearch = null;
  return pending;
}

function statusLabel(t, status) {
  return t("StringsScreen", STATUS_LABELS[status] ?? status);
}

function buildSourceHtml(source, spans) {
  const sorted = [...spans].sort((a, b) => a[0] - b[0]);
  let cursor = 0;
  let html = "";
  for (const [start, end, settled] of sorted) {
    if (start > cursor) html += escapeHtml(source.slice(cursor, start));
    // The name rides on the term itself: the translator is already looking
    // here, and the alternative is a trip to the glossary screen.
    const tip = settled ? ` data-tip="${escapeHtml(settled)}"` : "";
    html += `<u${tip}>${escapeHtml(source.slice(start, end))}</u>`;
    cursor = end;
  }
  html += escapeHtml(source.slice(cursor));
  return html;
}

// A game script lays its dialogue out with ideographic spaces and newlines: one
// line of this game's credits is fourteen of them before the word starts. On a
// single line of a list that padding is all a translator sees, so the preview
// collapses it. The stored string keeps every space it came with.
function preview(text) {
  return String(text ?? "").replace(/\s+/gu, " ").trim();
}

function imageRegions(unit) {
  if (!Array.isArray(unit.regions)) return [];
  return unit.regions.filter(
    (region) => Array.isArray(region?.box) && region.box.length === 4,
  );
}

function statusChip(t, status) {
  return el("span", { class: `status-chip status-${status}` }, [
    el("span", { class: "status-dot" }),
    statusLabel(t, status),
  ]);
}

function projectLang(ctx, key) {
  return htmlLang(ctx.appState.project?.[key]);
}

function sameUnit(left, right) {
  return left.source_file === right.source_file && left.id === right.id;
}

function unitRowMain(ctx, row) {
  const { t } = ctx;
  const regions = imageRegions(row);
  return el("div", { class: "unit-row-main" }, [
    // The source is what a translator scans down; the identifier is a
    // reference they look at once they have stopped. Putting the id first
    // spent the whole line on "pylm:text:00000001.lsb:137:0" and left
    // "エン..." of the sentence.
    el("div", { class: "unit-row-source" }, [
      el("span", {
        class: "unit-row-text",
        lang: projectLang(ctx, "source_lang"),
        text: preview(row.source),
      }),
      el("span", { class: "unit-row-marks" }, [
        row.kind === "image"
          ? el("span", { class: "unit-kind-badge", text: t("Strings", "Image") })
          : null,
        regions.length
          ? el("span", { class: "unit-bbox-count", text: `BBox ${regions.length}` })
          : null,
      ]),
    ]),
    el("div", {
      class: row.target ? "unit-row-target" : "unit-row-target unit-row-target-empty",
      lang: row.target ? projectLang(ctx, "target_lang") : null,
      text: row.target
        ? preview(row.target)
        : statusLabel(t, row.status === "ignored" ? "ignored" : "untranslated"),
    }),
  ]);
}

function dockEmpty(text) {
  return el("p", { class: "dock-empty", text });
}

export function mount(container, ctx) {
  const screen = new StringsScreen(ctx);
  container.replaceChildren(screen.element);
  screen.editor.clear();
  const refresh = () => screen.refresh();
  refresh().catch((err) => ctx.showError(err));

  return {
    refresh,
    unmount() {
      screen.dispose();
    },
  };
}

class UnitQueue {
  constructor(ctx, screen) {
    const { t } = ctx;
    this.ctx = ctx;
    this.screen = screen;
    this.filters = { status: "", q: "", scope: "source", category: "", source_lang: "" };
    this.selectedIndex = null; // position in the filtered queue, for "# n / total"
    this.filteredTotal = 0;
    this.page = 0;
    this.pageSize = PAGE_SIZES[0];
    this.renderedRows = []; // [{ node, row }] for the page on screen
    this.renderedOffset = 0; // queue position of the first rendered row
    this.cursor = null; // where the keyboard has taken the queue, until it has arrived
    this.settling = false;
    this.searchDebounce = null;
    const toolbar = this.toolbar();
    this.viewport = el("div", {
      class: "unit-viewport",
      role: "listbox",
      "aria-label": t("Strings", "Strings"),
    });
    this.viewport.addEventListener("keydown", (event) => this.onKeydown(event));
    this.element = el("div", { class: "unit-queue" }, [toolbar, this.viewport, this.foot()]);
  }

  get editor() {
    return this.screen.editor;
  }

  // -- queue toolbar: filters, search, status tabs, progress bar -----------

  toolbar() {
    const { t } = this.ctx;
    // The query reads against one thing: the source, the target, or the
    // identifier an engine and a bug report name a line by. Searching more at
    // once means an id like "0001" also answers with every sentence that happens
    // to contain it.
    this.searchScope = el("select", { class: "queue-scope", "aria-label": t("StringsScreen", "Search in") }, [
      el("option", { value: "source", text: t("StringsScreen", "Source") }),
      el("option", { value: "target", text: t("StringsScreen", "Target") }),
      el("option", { value: "id", text: t("StringsScreen", "Unit ID") }),
    ]);
    this.search = el("input", {
      type: "text",
      placeholder: t("StringsScreen", "Search strings"),
      "aria-label": t("StringsScreen", "Search strings"),
    });
    const filterRow = el("div", { class: "queue-filters" }, this.filterSelects());
    const searchRow = el("div", { class: "queue-search" }, [this.searchScope, this.search]);
    this.statusTabButtons = STATUS_TAB_ORDER.map((status) => this.statusTab(status));
    const statusTabs = el("div", {
      class: "status-tabs",
      role: "group",
      "aria-label": t("StringsScreen", "Filter by status"),
    }, this.statusTabButtons);
    this.listenToFilters();
    return el("div", { class: "queue-toolbar" }, [filterRow, searchRow, statusTabs, this.progressBar()]);
  }

  filterSelects() {
    const { t } = this.ctx;
    // Story apart from lines a variable is inserted into: those are checked token
    // by token, and among thousands of plain lines the few that carry one were lost.
    this.categorySelect = el("select", { class: "queue-scope", "aria-label": t("StringsScreen", "Category") }, [
      el("option", { value: "", text: t("StringsScreen", "All categories") }),
      el("option", { value: "story", text: t("StringsScreen", "Story") }),
      el("option", { value: "variables", text: t("StringsScreen", "With variables") }),
      el("option", { value: "values", text: t("StringsScreen", "Values") }),
      el("option", { value: "images", text: t("StringsScreen", "Pictures") }),
    ]);
    this.languageSelect = createLanguageSelect(this.ctx, {
      id: "queue-source-language",
      label: t("ProjectSettings", "Source language"),
      languages: [],
    });
    this.languageSelect.classList.add("queue-scope");
    this.allLanguages = el("option", { value: "", text: t("StringsScreen", "All languages") });
    this.languageSelect.prepend(this.allLanguages);
    this.languageSelect.value = "";
    return [this.categorySelect, this.languageSelect];
  }

  statusTab(status) {
    const { t } = this.ctx;
    const label = status === "" ? t("StringsScreen", "All") : statusLabel(t, status);
    const button = el("button", {
      class: status === "" ? "status-tab active" : "status-tab",
      type: "button",
      "aria-pressed": String(status === ""),
    }, [
      // The dot is the same colour the row's mark and the editor's chip use, so
      // a filter and the rows it leaves behind read as one thing.
      el("span", { class: `status-tab-dot status-${status || "all"}` }),
      label,
    ]);
    button.addEventListener("click", () => {
      this.filters.status = status;
      this.markStatusTab(button);
      this.reload();
    });
    return button;
  }

  markStatusTab(chosen) {
    for (const button of this.statusTabButtons) {
      button.classList.toggle("active", button === chosen);
      button.setAttribute("aria-pressed", String(button === chosen));
    }
  }

  progressBar() {
    this.progressTranslatedFill = el("div", { class: "progress-fill progress-fill-translated" });
    this.progressReviewedFill = el("div", { class: "progress-fill progress-fill-reviewed" });
    this.progressLabel = el("span", { class: "progress-label muted" });
    return el("div", { class: "progress-bar-wrap" }, [
      el("div", { class: "progress-track" }, [this.progressTranslatedFill, this.progressReviewedFill]),
      this.progressLabel,
    ]);
  }

  listenToFilters() {
    this.search.addEventListener("input", () => {
      this.filters.q = this.search.value.trim();
      clearTimeout(this.searchDebounce);
      this.searchDebounce = setTimeout(() => {
        this.reload();
      }, SEARCH_DEBOUNCE_MS);
    });
    this.searchScope.addEventListener("change", () => {
      this.filters.scope = this.searchScope.value;
      if (this.filters.q) this.reload();
    });
    this.categorySelect.addEventListener("change", () => {
      this.filters.category = this.categorySelect.value;
      this.reload();
    });
    this.languageSelect.addEventListener("change", () => {
      this.filters.source_lang = this.languageSelect.value;
      this.reload();
    });
  }

  foot() {
    const { t } = this.ctx;
    this.pagerButtons = PAGER_STEPS.map(({ glyph, label, to }) => el("button", {
      class: "queue-pager-button",
      type: "button",
      "aria-label": t("StringsScreen", label),
      title: t("StringsScreen", label),
      text: glyph,
      onClick: () => this.goToPage(to(this.page, this.pageCount())),
    }));
    this.queueCount = el("span", { class: "queue-foot-count mono" });
    this.pageSizeSelect = el(
      "select",
      { class: "queue-page-size", "aria-label": t("StringsScreen", "Rows per page") },
      PAGE_SIZES.map((size) => el("option", { value: String(size), text: String(size) })),
    );
    this.pageSizeSelect.addEventListener("change", () => {
      // Keep the row that was at the top of the old page in view rather than
      // jumping back to the start of the queue.
      const firstRow = this.page * this.pageSize;
      this.pageSize = Number(this.pageSizeSelect.value);
      this.page = Math.floor(firstRow / this.pageSize);
      this.loadPage().catch((err) => this.ctx.showError(err));
    });
    return el("div", { class: "queue-foot" }, [...this.pagerButtons, this.queueCount, this.pageSizeSelect]);
  }

  isSelected(row) {
    return this.editor.isSelected(row);
  }

  renderRow(row, index, tabStop) {
    const { t } = this.ctx;
    const node = el("div", {
      class: `unit-row status-${row.status}`,
      role: "option",
      tabindex: tabStop ? "0" : "-1",
      "aria-selected": "false",
      "aria-setsize": String(this.filteredTotal),
      "aria-posinset": String(index + 1),
    }, [
      // One glyph carries the status: a row is scanned down a column, and a
      // mark in a fixed column reads faster there than a labelled chip.
      el("span", {
        class: "unit-row-mark",
        role: "img",
        "aria-label": statusLabel(t, row.status),
        text: STATUS_MARKS[row.status] ?? "■",
      }),
      unitRowMain(this.ctx, row),
    ]);
    if (this.isSelected(row)) {
      node.classList.add("selected");
      node.setAttribute("aria-selected", "true");
    }
    node.addEventListener("click", () => {
      this.cursor = null;
      this.chooseRow(row, index).catch((err) => this.ctx.showError(err));
    });
    return node;
  }

  focusSelectedRow() {
    const stay = this.renderedRows.find(({ row }) => this.isSelected(row));
    if (stay) this.focusRow(stay.node);
  }

  // The draft being edited is saved before the editor reads a unit again; a
  // save that fails keeps the editor, and its error, where they are.
  async chooseRow(row, index) {
    if (this.editor.selection !== null && !(await this.editor.saveDraft())) {
      if (!this.ctx.conflictPending?.()) this.focusSelectedRow();
      return;
    }
    // The save rebuilt the page, so the row may sit somewhere else by now.
    const at = this.renderedRows.findIndex(({ row: shown }) => sameUnit(shown, row));
    this.selectedIndex = at === -1 ? index : this.renderedOffset + at;
    await this.editor.selectUnit(row.source_file, row.id);
  }

  // Exactly one row is in the tab order, so Tab enters the queue once and
  // leaves it again; the arrow keys move around inside.
  setActiveRow(active) {
    for (const { node } of this.renderedRows) node.tabIndex = node === active ? 0 : -1;
  }

  updateRenderedSelection() {
    this.renderedRows.forEach(({ node, row }) => {
      const selected = this.isSelected(row);
      node.classList.toggle("selected", selected);
      node.setAttribute("aria-selected", String(selected));
      if (selected && !this.viewport.contains(document.activeElement)) this.setActiveRow(node);
    });
  }

  focusRow(node) {
    this.setActiveRow(node);
    node.focus({ preventScroll: true });
    node.scrollIntoView({ block: "nearest" });
  }

  // A page is rebuilt after every save and every landed batch; a reader who is
  // arrowing down it keeps their place, on the row that moved or the one that
  // took its position.
  restoreFocus(row, at, samePage) {
    const rows = this.renderedRows;
    const kept =
      rows.find((entry) => sameUnit(entry.row, row)) ??
      (samePage ? rows[Math.min(at, rows.length - 1)] : undefined);
    if (!kept) return;
    this.setActiveRow(kept.node);
    kept.node.focus({ preventScroll: true });
  }

  pageRows() {
    const rowHeight = this.renderedRows[0]?.node.offsetHeight || 1;
    return Math.max(1, Math.floor(this.viewport.clientHeight / rowHeight) - 1);
  }

  async openIndex(index) {
    const targetPage = Math.floor(index / this.pageSize);
    if (targetPage !== this.page) {
      this.page = targetPage;
      await this.loadPage();
    }
    const entry = this.renderedRows[index - this.renderedOffset];
    if (!entry) return;
    this.focusRow(entry.node);
    await this.chooseRow(entry.row, index);
  }

  // Keys that arrive while a unit is still loading only move the target on, so
  // holding an arrow down opens the unit it ends on rather than every unit it
  // passes. The draft being edited is saved before the editor moves off it, and
  // a save that fails keeps the editor where it is.
  async settleCursor() {
    let shown = null;
    try {
      while (this.cursor !== null && this.cursor !== shown) {
        const index = this.cursor;
        if (this.editor.selection !== null && !(await this.editor.saveDraft())) {
          this.focusSelectedRow();
          return;
        }
        if (index !== this.cursor) continue;
        await this.openIndex(index);
        shown = index;
      }
    } catch (err) {
      this.ctx.showError(err);
    } finally {
      this.cursor = null;
      this.settling = false;
    }
  }

  moveCursor(index) {
    this.cursor = index;
    const entry = this.renderedRows[index - this.renderedOffset];
    if (entry) this.focusRow(entry.node);
    if (this.settling) return;
    this.settling = true;
    this.settleCursor();
  }

  onKeydown(event) {
    if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
    const at = this.renderedRows.findIndex(({ node }) => node === event.target);
    if (at === -1) return;
    const { row } = this.renderedRows[at];
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const chosen = this.isSelected(row) ? Promise.resolve() : this.chooseRow(row, this.renderedOffset + at);
      chosen
        .then(() => {
          if (event.key === "Enter" && !this.ctx.conflictPending?.()) this.editor.targetBox.focus();
        })
        .catch((err) => this.ctx.showError(err));
      return;
    }
    const to = listTarget(event.key, this.cursor ?? this.renderedOffset + at, this.filteredTotal, this.pageRows());
    if (to === null) return;
    event.preventDefault();
    this.moveCursor(to);
  }

  pageCount() {
    return Math.max(1, Math.ceil(this.filteredTotal / this.pageSize));
  }

  goToPage(target) {
    const clamped = Math.max(0, Math.min(this.pageCount() - 1, target));
    if (clamped === this.page) return;
    this.page = clamped;
    this.loadPage().catch((err) => this.ctx.showError(err));
  }

  fetchRows(offset, limit) {
    const { filters } = this;
    return this.ctx.bridge.unit_rows(
      filters.status || null,
      filters.q || null,
      offset,
      limit,
      filters.scope,
      filters.category || null,
      filters.source_lang || null,
    );
  }

  // The page is built whole and swapped in one go: replacing rows as each one
  // arrives leaves the queue half-empty for as long as the read takes.
  async loadPage() {
    const offset = this.page * this.pageSize;
    const result = await this.fetchRows(offset, this.pageSize);
    this.filteredTotal = result.total;
    if (offset && offset >= result.total) {
      this.page = this.pageCount() - 1;
      await this.loadPage();
      return;
    }
    this.showPage(result.rows, offset);
    this.updatePager();
    this.editor.updateMetaPosition();
  }

  showPage(rows, offset) {
    const focusedAt = this.renderedRows.findIndex(({ node }) => node === document.activeElement);
    const focusedRow = focusedAt === -1 ? null : this.renderedRows[focusedAt].row;
    const samePage = this.renderedOffset === offset;
    const tabStop = Math.max(rows.findIndex((row) => this.isSelected(row)), 0);
    this.renderedRows = rows.map((row, at) => ({
      row,
      node: this.renderRow(row, offset + at, at === tabStop),
    }));
    this.renderedOffset = offset;
    this.viewport.replaceChildren(...this.renderedRows.map((entry) => entry.node));
    if (focusedRow) this.restoreFocus(focusedRow, focusedAt, samePage);
    // The position counts from the queue as it stands now, so it is read back
    // off the page rather than remembered from the click that opened the unit.
    const at = this.renderedRows.findIndex(({ row }) => this.isSelected(row));
    if (at !== -1) this.selectedIndex = offset + at;
  }

  updatePager() {
    const { t } = this.ctx;
    const pages = this.pageCount();
    const [first, previous, next, last] = this.pagerButtons;
    first.disabled = this.page === 0;
    previous.disabled = this.page === 0;
    next.disabled = this.page >= pages - 1;
    last.disabled = this.page >= pages - 1;
    this.queueCount.textContent = `${t("StringsScreen", "{page} / {pages}", {
      page: this.page + 1,
      pages,
    })} · ${t("StringsScreen", "{count} units", { count: this.filteredTotal })}`;
  }

  // Filters and the query change which units there are, so the queue starts
  // again from its first page rather than holding a page that may not exist.
  reload() {
    this.page = 0;
    // A different queue means the old ordinal counts nothing; loadPage finds it
    // again if the open unit is still in the filtered set.
    this.selectedIndex = null;
    this.loadPage().catch((err) => this.ctx.showError(err));
  }

  applySearch(query, scope) {
    this.filters = { status: "", q: query, scope, category: "", source_lang: "" };
    this.search.value = query;
    this.searchScope.value = scope;
    this.categorySelect.value = "";
    this.languageSelect.value = "";
    this.markStatusTab(this.statusTabButtons[0]);
    this.page = 0;
    this.selectedIndex = null;
  }

  async focusQaTarget(target) {
    this.applySearch(target.id, "id");
    const index = await findUnitIndex(
      (offset, limit) => this.ctx.bridge.unit_rows(null, target.id, offset, limit, "id"),
      target,
    );
    if (index === -1) {
      throw new Error(`Could not find QA target ${target.source_file} / ${target.id}.`);
    }
    this.page = Math.floor(index / this.pageSize);
    this.selectedIndex = index;
    await this.loadPage();
    await this.editor.selectUnit(target.source_file, target.id);
  }

  async refreshLanguages() {
    const languages = await this.ctx.bridge.unit_languages();
    const options = createLanguageSelect(this.ctx, { languages });
    this.languageSelect.replaceChildren(this.allLanguages, ...options.options);
    if (!languages.includes(this.filters.source_lang)) this.filters.source_lang = "";
    this.languageSelect.value = this.filters.source_lang;
  }

  async refreshProgress() {
    const result = await this.ctx.bridge.unit_counts();
    const counts = result.status_counts;
    const total = result.total;
    const translated = total - counts.untranslated;
    const reviewed = counts.reviewed;
    this.progressTranslatedFill.style.width = `${total ? (translated / total) * 100 : 0}%`;
    this.progressReviewedFill.style.width = `${total ? (reviewed / total) * 100 : 0}%`;
    this.progressLabel.textContent = this.ctx.t("StringsScreen", "{translated} / {total} translated", { translated, total });
  }
}

class UnitAsset {
  constructor(ctx, { onLayout, onOpenEditor }) {
    const { t } = ctx;
    this.ctx = ctx;
    this.onLayout = onLayout;
    this.regions = [];
    this.image = "";
    this.preview = el("img", { id: "unit-asset-preview", alt: "", hidden: true });
    this.regionLayer = el("div", { class: "unit-asset-regions" });
    this.stage = el("div", { class: "unit-asset-stage", hidden: true }, [this.preview, this.regionLayer]);
    this.hint = el("p", { class: "muted unit-asset-hint", hidden: true });
    // The boxes are the picture's own coordinates, and the only place they can be
    // moved is the editor that draws on it. A box is a link to it as much as the
    // button is: the translator is already pointing at the one that is wrong.
    this.openEditor = onOpenEditor;
    this.block = el("section", { class: "editor-block unit-asset-block", hidden: true }, [
      this.stage,
      this.hint,
      el("button", {
        class: "btn btn-sm unit-asset-edit",
        type: "button",
        text: t("UnitEditor", "Edit picture and BBoxes"),
        onClick: () => this.openEditor(),
      }),
    ]);
    this.translationHint = el("p", { class: "muted unit-bbox-translation", hidden: true });
    this.preview.addEventListener("load", () => this.renderRegions());
  }

  hide() {
    this.regions = [];
    this.image = "";
    this.regionLayer.replaceChildren();
    this.block.hidden = true;
    this.stage.hidden = true;
    this.preview.hidden = true;
    this.preview.removeAttribute("src");
    this.preview.alt = "";
    this.hint.textContent = "";
    this.hint.hidden = true;
    this.onLayout(false);
    this.translationHint.textContent = "";
    this.translationHint.hidden = true;
  }

  renderRegions() {
    this.regionLayer.replaceChildren();
    const width = this.preview.naturalWidth;
    const height = this.preview.naturalHeight;
    if (!width || !height) return;
    this.regions.forEach((region, index) => {
      const node = this.regionButton(region, index, width, height);
      if (node) this.regionLayer.append(node);
    });
  }

  regionButton(region, index, width, height) {
    const [x0, y0, x1, y1] = region.box.map(Number);
    if (![x0, y0, x1, y1].every(Number.isFinite)) return null;
    const left = Math.max(0, Math.min(100, (x0 / width) * 100));
    const top = Math.max(0, Math.min(100, (y0 / height) * 100));
    const right = Math.max(left, Math.min(100, (x1 / width) * 100));
    const bottom = Math.max(top, Math.min(100, (y1 / height) * 100));
    const source = region.text ?? "";
    const label = `BBox ${index + 1} [${region.box.join(", ")}]${source ? `: ${source}` : ""}`;
    const node = el("button", {
      class: "unit-asset-region",
      type: "button",
      title: label,
      "aria-label": label,
      onclick: () => this.openEditor(),
    }, [el("span", { class: "unit-asset-region-index", text: String(index + 1) })]);
    node.style.left = `${left}%`;
    node.style.top = `${top}%`;
    node.style.width = `${right - left}%`;
    node.style.height = `${bottom - top}%`;
    return node;
  }

  setRegions(unit) {
    this.regions = imageRegions(unit);
    if (!this.regions.length) return;
    this.translationHint.textContent = this.ctx.t(
      "UnitEditor",
      "{count} BBox regions. Translate processes image units too; keep one target line per BBox in this order.",
      { count: this.regions.length },
    );
    this.translationHint.hidden = false;
  }

  async show(unit, isCurrent) {
    this.hide();
    if (unit.kind !== "image" || !unit.image) return;
    this.image = unit.image;
    this.setRegions(unit);
    try {
      const result = await this.ctx.bridge.image_data(unit.image);
      if (!isCurrent()) return;
      this.block.hidden = false;
      this.stage.hidden = false;
      this.onLayout(true);
      this.preview.alt = unit.image;
      this.preview.src = `data:image/png;base64,${result.data}`;
      this.preview.hidden = false;
    } catch (error) {
      if (!isCurrent()) return;
      this.block.hidden = false;
      this.onLayout(true);
      this.hint.textContent = this.ctx.t("UnitEditor", "Preview unavailable: {error}", {
        error: error.message,
      });
      this.hint.hidden = false;
    }
  }
}

// The dock answers "what does the project already know about this line" --
// what it has been, what other lines settled for, what its terms are called,
// and what QA said about it. Memory is on the page as well as here: the page
// is where it is read while typing, the dock is where it is browsed.
class UnitDock {
  constructor(ctx) {
    const { t } = ctx;
    this.ctx = ctx;
    this.historyList = el("div", { class: "dock-panel" });
    this.memoryPanel = el("div", { class: "dock-panel", hidden: true });
    this.glossaryPanel = el("div", { class: "dock-panel", hidden: true });
    this.qualityPanel = el("div", { class: "dock-panel", hidden: true });
    this.panels = [
      { key: "history", label: t("UnitEditor", "History"), panel: this.historyList },
      { key: "memory", label: t("UnitEditor", "Memory"), panel: this.memoryPanel },
      { key: "glossary", label: t("UnitEditor", "Glossary"), panel: this.glossaryPanel },
      { key: "quality", label: t("UnitEditor", "Quality"), panel: this.qualityPanel },
    ];
    this.tabButtons = this.panels.map((entry) => el("button", {
      class: "dock-tab",
      type: "button",
      role: "tab",
      "aria-selected": entry.key === "history" ? "true" : "false",
      text: entry.label,
      onClick: () => this.show(entry.key),
    }));
    // What was written before sits beside the line rather than under the box
    // being typed into; the suggestions stay on the source, where it is read.
    this.element = el("aside", { class: "strings-dock" }, [
      el("div", { class: "dock-block" }, [
        el("div", { class: "dock-tabs", role: "tablist" }, this.tabButtons),
        el("div", { class: "dock-body" }, this.panels.map(({ panel }) => panel)),
      ]),
    ]);
  }

  show(key) {
    this.panels.forEach((entry, index) => {
      entry.panel.hidden = entry.key !== key;
      this.tabButtons[index].setAttribute("aria-selected", entry.key === key ? "true" : "false");
    });
  }

  clear() {
    this.memoryPanel.replaceChildren();
    this.historyList.replaceChildren();
    this.glossaryPanel.replaceChildren();
    this.qualityPanel.replaceChildren();
  }

  renderMemory(suggestions, suggestionItem) {
    setChildren(this.memoryPanel, [
      suggestions.length === 0
        ? dockEmpty(this.ctx.t("UnitEditor", "Nothing in memory for this line."))
        : null,
      ...suggestions.map(suggestionItem),
    ]);
  }

  renderHistory(history) {
    const { t } = this.ctx;
    setChildren(this.historyList, [...history].reverse().map((entry) =>
      el("div", { class: "history-item" }, [
        statusChip(t, entry.status),
        el("span", { class: "history-target", lang: projectLang(this.ctx, "target_lang"), text: entry.target }),
        el("span", { class: "history-at muted", text: new Date(entry.at).toLocaleString() }),
      ])));
  }

  // The spans the source is underlined from, gathered per term: the same term
  // twice in one line is one decision used twice, not two decisions.
  renderGlossary(unit) {
    const terms = new Map();
    for (const [start, end, target] of unit.spans) {
      const source = unit.source.slice(start, end);
      const seen = terms.get(source);
      if (seen) seen.count += 1;
      else terms.set(source, { source, target, count: 1 });
    }
    if (terms.size === 0) {
      setChildren(this.glossaryPanel, dockEmpty(this.ctx.t("UnitEditor", "No glossary term in this line.")));
      return;
    }
    setChildren(this.glossaryPanel, [...terms.values()].map((term) =>
      el("div", { class: "dock-term" }, [
        el("span", {
          class: "dock-term-source",
          lang: projectLang(this.ctx, "source_lang"),
          text: term.source,
          title: term.source,
        }),
        el("span", {
          class: "dock-term-target",
          lang: projectLang(this.ctx, "target_lang"),
          text: term.target,
          title: term.target,
        }),
        el("span", { class: "dock-term-count", text: `x${term.count}` }),
      ])));
  }

  // The findings for one line, and the summary its quality fact gives of them.
  renderQuality(sourceFile, id) {
    const { t } = this.ctx;
    const report = this.ctx.appState.qaReport;
    if (!report) {
      setChildren(this.qualityPanel, dockEmpty(t("UnitEditor", "QA has not run.")));
      return t("UnitEditor", "QA has not run.");
    }
    const hits = report.issues.filter((issue) => issue.file === sourceFile && issue.unit_id === id);
    if (hits.length === 0) {
      setChildren(this.qualityPanel, dockEmpty(t("UnitEditor", "No quality findings for this line.")));
      return t("UnitEditor", "No quality findings for this line.");
    }
    setChildren(this.qualityPanel, hits.map((hit) =>
      el("div", { class: "dock-hit", "data-severity": hit.severity ?? "error" }, [
        el("span", { class: "dock-hit-check", text: checkLabel(t, hit.check) }),
        el("span", { class: "dock-hit-message", text: issueText(t, hit) }),
      ])));
    return hits.map((hit) => checkLabel(t, hit.check)).join(", ");
  }
}

class UnitEditor {
  constructor(ctx, screen) {
    this.ctx = ctx;
    this.screen = screen;
    this.selection = null; // { source_file, id }
    this.requestToken = 0;
    this.currentSourceText = "";
    this.currentStatus = null;
    this.actionInProgress = false;
    this.pendingDraftSave = null;
    this.savedTarget = ""; // the selected unit's target as last read or saved
    this.closeStatusMenu = () => {
      this.statusMenu.hidden = true;
      this.statusMenuButton.setAttribute("aria-expanded", "false");
    };
    const metadata = this.metadataLine();
    const sourceBlock = this.sourceBlock();
    this.asset = new UnitAsset(ctx, {
      onLayout: (hasAsset) => this.primary.classList.toggle("no-asset", !hasAsset),
      onOpenEditor: () => this.openImageEditor(),
    });
    const editorCopy = el("div", { class: "unit-editor-copy" }, [
      sourceBlock,
      this.targetBlock(),
      this.factList(),
      this.actionBar(),
      this.memoryBlock(),
    ]);
    this.primary = el("div", { class: "unit-editor-primary no-asset" }, [this.asset.block, editorCopy]);
    this.element = el("div", { class: "unit-editor" }, [metadata, this.primary]);
    this.dock = new UnitDock(ctx);
    this.listenToTarget();
  }

  get queue() {
    return this.screen.queue;
  }

  metadataLine() {
    this.metaStatus = el("span", { class: "unit-meta-status" });
    this.metaModified = el("span", { class: "unit-meta-modified mono" });
    this.metaPosition = el("span", { class: "unit-meta-position mono" });
    return el("div", { class: "unit-meta-line" }, [
      this.metaStatus,
      this.metaModified,
      el("span", { class: "unit-meta-gap" }),
      this.metaPosition,
    ]);
  }

  // The source is the line itself, so it is given as the line and nothing else:
  // no label above it, no toolbar beside it. What can be done with it lives in
  // the fact list below, where the rest of the unit's particulars already are.
  sourceBlock() {
    const { t } = this.ctx;
    this.sourceBox = el("div", {
      id: "unit-source",
      class: "mono source-box",
      tabindex: "0",
      role: "textbox",
      "aria-readonly": "true",
      "aria-multiline": "true",
      "aria-label": t("UnitEditor", "Source"),
    });
    this.copyButton = el("button", { class: "btn btn-sm", type: "button", text: t("UnitEditor", "Copy") });
    this.copyButton.addEventListener("click", () => this.copySource());
    return el("div", { class: "editor-block source-block" }, [this.sourceBox]);
  }

  targetBlock() {
    const { t } = this.ctx;
    this.targetBox = el("textarea", {
      id: "unit-target",
      rows: "6",
      "aria-label": t("UnitEditor", "Target"),
    });
    this.saveError = el("p", { class: "severity-error", hidden: true });
    return el("div", { class: "editor-block" }, [this.asset.translationHint, this.targetBox, this.saveError]);
  }

  // A split button: the review decision is the one taken over and over, so it
  // stays one click, and handing the line back -- to a draft, or to be
  // translated again -- sits behind the caret rather than as two more buttons
  // competing with it.
  statusSplit() {
    const { t } = this.ctx;
    this.markReviewedButton = el("button", {
      class: "btn btn-primary split-main",
      type: "button",
      disabled: true,
      text: t("UnitEditor", "Mark reviewed"),
      onClick: () => this.toggleReviewed(),
    });
    this.statusMenuButton = el("button", {
      class: "btn btn-primary split-toggle",
      type: "button",
      disabled: true,
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      "aria-label": t("UnitEditor", "More status actions"),
      text: "⌄",
    });
    this.statusMenu = el("div", { class: "split-menu", role: "menu", hidden: true }, [
      { status: "mt", label: t("UnitEditor", "Mark machine-translated") },
      { status: "untranslated", label: t("UnitEditor", "Mark untranslated") },
      { status: "ignored", label: t("UnitEditor", "Mark ignored") },
    ].map((entry) => el("button", {
      class: "split-menu-item",
      type: "button",
      role: "menuitem",
      text: entry.label,
      onClick: () => {
        this.closeStatusMenu();
        this.setStatus(entry.status);
      },
    })));
    this.listenToStatusMenu();
    return el("div", { class: "split-button" }, [this.markReviewedButton, this.statusMenuButton, this.statusMenu]);
  }

  listenToStatusMenu() {
    this.statusMenuButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = this.statusMenu.hidden;
      this.statusMenu.hidden = !open;
      this.statusMenuButton.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", this.closeStatusMenu);
    this.statusMenu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this.closeStatusMenu();
    });
  }

  factList() {
    const { t } = this.ctx;
    const value = () => el("dd", { class: "unit-fact-value mono" });
    const fact = (label, node, aside) => [
      el("dt", { class: "unit-fact-label", text: label }),
      node,
      el("dd", { class: "unit-fact-aside" }, aside ? [aside] : []),
    ];
    this.factUnitId = value();
    this.factSourceFile = value();
    this.factLocation = value();
    this.factLength = value();
    this.factQuality = value();
    // Only a script line has one (`<script name>:<line>`): the unit ID names the
    // script by index alone, so this is how a translator finds which script it is.
    this.locationFact = fact(t("UnitEditor", "Location"), this.factLocation);
    this.showLocation("");
    return el("dl", { class: "unit-facts" }, [
      fact(t("UnitEditor", "Unit ID"), this.factUnitId, this.copyButton),
      fact(t("UnitEditor", "Source file"), this.factSourceFile),
      this.locationFact,
      fact(t("UnitEditor", "Length"), this.factLength),
      fact(t("UnitEditor", "Quality"), this.factQuality),
    ].flat());
  }

  showLocation(scene) {
    this.factLocation.textContent = scene;
    this.factLocation.title = scene;
    for (const node of this.locationFact) node.hidden = !scene;
  }

  actionBar() {
    const { t } = this.ctx;
    this.saveButton = el("button", {
      class: "btn", type: "button", disabled: true, text: t("UnitEditor", "Save"), onClick: () => this.saveDraft(),
    });
    const statusSplit = this.statusSplit();
    this.nextButton = el("button", {
      class: "btn", type: "button", disabled: true, text: t("UnitEditor", "Next"), onClick: () => this.goToNext(),
    });
    this.markReviewedError = el("p", { class: "severity-error" });
    return el("div", { class: "editor-actions" }, [
      el("div", { class: "button-bar" }, [this.saveButton, statusSplit, this.nextButton]),
      this.markReviewedError,
    ]);
  }

  memoryBlock() {
    this.memoryCount = el("span", { class: "memory-count mono" });
    this.sourceSuggestions = el("div", { class: "source-suggestions", hidden: true });
    this.memory = el("section", { class: "editor-block memory-block", hidden: true }, [
      el("div", { class: "memory-head" }, [
        el("span", { class: "memory-title", text: this.ctx.t("UnitEditor", "Similar translations") }),
        el("span", { class: "unit-meta-gap" }),
        this.memoryCount,
      ]),
      this.sourceSuggestions,
    ]);
    return this.memory;
  }

  listenToTarget() {
    const box = this.targetBox;
    box.addEventListener("input", () => {
      if (this.selection === null) return;
      this.updateActions();
      this.ctx.bridge.begin_edit(this.selection.source_file, this.selection.id, box.value, this.savedTarget)
        .catch((err) => this.ctx.showError(err));
    });
    box.addEventListener("blur", () => this.saveDraft());
    box.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        this.toggleReviewed();
      }
    });
  }

  isSelected(candidate) {
    return this.selection !== null && sameUnit(this.selection, candidate);
  }

  updateActions() {
    const isReviewed = this.currentStatus === "reviewed";
    const idle = this.selection === null || this.actionInProgress;
    this.markReviewedButton.textContent = this.ctx.t(
      "UnitEditor",
      isReviewed ? "Mark unreviewed" : "Mark reviewed",
    );
    this.saveButton.disabled = idle;
    // Reviewing needs something to review; handing the line back does not, so
    // the caret stays live on a unit whose target was just emptied.
    this.markReviewedButton.disabled = idle || (!this.targetBox.value && !isReviewed);
    this.statusMenuButton.disabled = idle;
    this.nextButton.disabled = idle;
  }

  updateMetaPosition() {
    const { selectedIndex, filteredTotal } = this.queue;
    this.metaPosition.textContent =
      selectedIndex === null
        ? ""
        : this.ctx.t("UnitEditor", "# {position} / {total}", {
            position: selectedIndex + 1,
            total: filteredTotal,
          });
  }

  clear() {
    this.selection = null;
    this.queue.selectedIndex = null;
    this.currentSourceText = "";
    clear(this.metaStatus);
    this.metaModified.textContent = "";
    this.metaPosition.textContent = "";
    this.factUnitId.textContent = "";
    this.factSourceFile.textContent = "";
    this.showLocation("");
    this.factLength.textContent = "";
    this.factQuality.textContent = "";
    this.asset.hide();
    this.sourceBox.innerHTML = "";
    this.targetBox.value = "";
    this.currentStatus = null;
    this.memory.hidden = true;
    this.memoryCount.textContent = "";
    this.sourceSuggestions.replaceChildren();
    this.sourceSuggestions.hidden = true;
    this.dock.clear();
    this.saveError.hidden = true;
    this.markReviewedError.textContent = "";
    this.updateActions();
  }

  async selectUnit(sourceFile, id, untouchedOnly = false) {
    const token = ++this.requestToken;
    this.selection = { source_file: sourceFile, id };
    this.queue.updateRenderedSelection();
    const unit = await this.ctx.bridge.unit_detail(sourceFile, id);
    if (token !== this.requestToken) return;
    if (untouchedOnly && this.targetBox.value !== this.savedTarget) return;
    this.showSource(unit, sourceFile, id);
    this.asset.show(unit, () => token === this.requestToken).catch(() => {});
    this.showState(unit);
    this.showSuggestions(unit.suggestions);
    this.dock.renderMemory(unit.suggestions, (suggestion) => this.suggestionItem(suggestion));
    this.dock.renderHistory(unit.history);
    this.dock.renderGlossary(unit);
    this.renderQuality(sourceFile, id);
  }

  showSource(unit, sourceFile, id) {
    const { appState } = this.ctx;
    this.currentSourceText = unit.source;
    this.factUnitId.textContent = id;
    this.factUnitId.title = id;
    this.factSourceFile.textContent = sourceFile;
    this.factSourceFile.title = sourceFile;
    this.showLocation(unit.scene ?? "");
    this.factLength.textContent = String(unit.source.length);
    this.sourceBox.innerHTML = buildSourceHtml(unit.source, unit.spans);
    applyLang(this.sourceBox, appState.project?.source_lang);
    applyLang(this.targetBox, appState.project?.target_lang);
  }

  showState(unit) {
    const { t } = this.ctx;
    this.targetBox.value = unit.target;
    this.savedTarget = unit.target;
    this.currentStatus = unit.status;
    this.metaStatus.replaceChildren(statusChip(t, unit.status));
    // A unit is stamped when it moves between states, so the last transition is
    // the only "modified" the project actually records.
    const lastEntry = unit.history[unit.history.length - 1];
    this.metaModified.textContent = lastEntry
      ? t("UnitEditor", "Changed {at}", { at: new Date(lastEntry.at).toLocaleString() })
      : "";
    this.updateMetaPosition();
    this.saveError.hidden = true;
    this.markReviewedError.textContent = "";
    this.updateActions();
  }

  showSuggestions(suggestions) {
    this.memory.hidden = suggestions.length === 0;
    this.memoryCount.textContent = this.ctx.t("UnitEditor", "{count} in memory", {
      count: suggestions.length,
    });
    this.sourceSuggestions.hidden = suggestions.length === 0;
    setChildren(this.sourceSuggestions, suggestions.map((suggestion) => this.suggestionItem(suggestion)));
  }

  renderQuality(sourceFile, id) {
    this.factQuality.textContent = this.dock.renderQuality(sourceFile, id);
  }

  // One card, built twice: on the page beside the line, and in the dock. Both
  // are the same offer, so both fill the target the same way.
  suggestionItem(suggestion) {
    const { t } = this.ctx;
    return el("div", { class: "suggestion-item" }, [
      el("div", { class: "suggestion-head" }, [
        el("span", {
          class: `suggestion-origin-badge origin-${suggestion.origin}`,
          text: t("UnitEditor", ORIGIN_LABELS[suggestion.origin] ?? suggestion.origin),
        }),
        suggestion.count > 1
          ? el("span", { class: "suggestion-count mono", text: `x${suggestion.count}` })
          : null,
        el("span", { class: "unit-meta-gap" }),
        el("button", {
          class: "btn btn-sm suggestion-use",
          type: "button",
          text: t("UnitEditor", "Use"),
          onClick: () => {
            this.targetBox.value = suggestion.target;
            this.targetBox.dispatchEvent(new Event("input"));
          },
        }),
      ]),
      el("div", {
        class: "suggestion-target",
        tabindex: "0",
        lang: projectLang(this.ctx, "target_lang"),
        text: suggestion.target,
      }),
    ]);
  }

  openImageEditor() {
    const image = this.asset.image;
    if (!image) return;
    const selected = this.selection === null ? null : { ...this.selection };
    this.screen.editorHost.open(image, {
      // The editor writes the same unit this queue is editing -- its boxes, its
      // source, its target -- so the row is read again rather than trusted.
      onSaved: () => {
        if (selected === null || !this.isSelected(selected)) return;
        this.selectUnit(selected.source_file, selected.id).catch((err) => this.ctx.showError(err));
        this.queue.loadPage().catch((err) => this.ctx.showError(err));
      },
    });
  }

  async copySource() {
    if (this.selection === null) return;
    try {
      await navigator.clipboard.writeText(this.currentSourceText);
    } catch {
      const scratch = el("textarea", { style: "position:fixed;opacity:0;" });
      scratch.value = this.currentSourceText;
      document.body.append(scratch);
      scratch.select();
      try {
        document.execCommand("copy");
      } catch {
        // best-effort fallback; clipboard access is not always available
      }
      scratch.remove();
    }
  }

  async saveDraft() {
    if (this.selection === null) return false;
    // The conflict dialog decides; clicking it blurs the box on the way.
    if (this.ctx.conflictPending?.()) return false;
    const selected = { ...this.selection };
    const target = this.targetBox.value;
    const baseline = this.savedTarget;
    // Next and review save first; rewriting an untouched line costs a whole
    // file write and would put back what a job changed under it.
    if (target === baseline) return true;
    const key = `${selected.source_file}\u0000${selected.id}\u0000${target}`;
    if (this.pendingDraftSave?.key === key) return this.pendingDraftSave.promise;
    const promise = this.writeDraft(selected, target, baseline);
    this.pendingDraftSave = { key, promise };
    try {
      return await promise;
    } finally {
      if (this.pendingDraftSave?.promise === promise) this.pendingDraftSave = null;
    }
  }

  async writeDraft(selected, target, baseline) {
    try {
      // The line as this box first showed it: if another program has moved
      // it since, the save is refused and the conflict dialog decides.
      await this.ctx.bridge.save_draft(selected.source_file, selected.id, target, baseline);
      if (this.isSelected(selected)) this.savedTarget = target;
      this.saveError.hidden = true;
      await this.queue.loadPage();
      // The target just changed, so the findings for it did too. Not
      // awaited: a large project takes seconds to check, and next or review
      // should not wait on it.
      this.ctx.refreshQuality().catch((err) => this.ctx.showError(err));
      return true;
    } catch (err) {
      this.saveError.textContent = err.message;
      this.saveError.hidden = false;
      return false;
    }
  }

  // Un-reviewing sends the line back to untranslated, not to a draft: taking
  // review away says the translation is not wanted, and `translate` only ever
  // touches untranslated units, so that is what puts it back in the queue.
  async toggleReviewed() {
    if (this.currentStatus === "reviewed") {
      await this.setStatus("untranslated");
      return;
    }
    await this.applyStatusChange((selected) =>
      this.ctx.bridge.mark_reviewed(selected.source_file, selected.id),
    );
  }

  async setStatus(status) {
    await this.applyStatusChange((selected) =>
      this.ctx.bridge.set_unit_status(selected.source_file, selected.id, status),
    );
  }

  // Whatever is done to the open unit waits for its draft to save, and is
  // dropped if the editor has moved to another unit in the meantime.
  async runUnitAction(work) {
    if (this.selection === null || this.actionInProgress) return;
    const selected = { ...this.selection };
    this.actionInProgress = true;
    this.updateActions();
    try {
      if (!(await this.saveDraft()) || !this.isSelected(selected)) return;
      await work(selected);
    } catch (err) {
      this.markReviewedError.textContent = err.message;
    } finally {
      this.actionInProgress = false;
      this.updateActions();
    }
  }

  applyStatusChange(change) {
    return this.runUnitAction(async (selected) => {
      await change(selected);
      this.markReviewedError.textContent = "";
      await this.selectUnit(selected.source_file, selected.id);
      await this.queue.loadPage();
      await this.queue.refreshProgress();
      this.ctx.refreshQuality().catch((err) => this.ctx.showError(err));
    });
  }

  goToNext() {
    return this.runUnitAction(async (selected) => {
      const { filters } = this.queue;
      const next = await this.ctx.bridge.next_unit(
        selected.source_file,
        selected.id,
        filters.status || null,
        filters.q || null,
        filters.scope,
        filters.category || null,
        filters.source_lang || null,
      );
      if (next === null) return;
      await this.selectUnit(next.source_file, next.id);
      this.targetBox.focus();
      this.markReviewedError.textContent = "";
    });
  }
}

class StringsScreen {
  constructor(ctx) {
    this.ctx = ctx;
    this.liveRefreshTimer = null;
    // The dialog for a conflict on the open line can be answered while another
    // screen is showing, so the answer is remembered here, not handed over.
    this.conflictOnSelection = false;
    const toolbar = this.toolbar();
    const importPanel = this.importPanel();
    this.queue = new UnitQueue(ctx, this);
    this.editor = new UnitEditor(ctx, this);
    this.element = el("div", { class: "strings-screen" }, [
      toolbar,
      importPanel,
      el("div", { class: "strings-workspace" }, [this.queue.element, this.editor.element, this.editor.dock.element]),
    ]);
    this.editorHost = createImageEditorHost(ctx);
    this.unsubscribe = ctx.onEvent((event) => this.onEvent(event));
    this.unsubscribeQuality = ctx.onQuality(() => {
      this.renderQualityCount();
      const { selection } = this.editor;
      if (selection !== null) this.editor.renderQuality(selection.source_file, selection.id);
    });
  }

  toolbar() {
    const { t } = this.ctx;
    const button = (text, onClick, className = "btn btn-sm") =>
      el("button", { class: className, type: "button", text, onClick });
    // A run is started, paused and restarted from the dashboard, not from here.
    this.translateButton = button(
      t("Strings", "Translate"), () => this.ctx.navigate("translation-dashboard"), "btn btn-sm btn-primary",
    );
    this.toolbarQuality = el("span", { class: "strings-toolbar-quality mono" });
    return el("div", { class: "strings-toolbar" }, [
      button(t("Strings", "Extract text"), () => this.runJob("extract")),
      this.translateButton,
      button(t("Strings", "Import"), () => {
        this.importPanelElement.hidden = !this.importPanelElement.hidden;
      }),
      button(t("Strings", "Transform"), () => this.transform()),
      el("span", { class: "unit-meta-gap" }),
      this.toolbarQuality,
    ]);
  }

  importPanel() {
    const { t } = this.ctx;
    this.importMapping = el("textarea", {
      rows: "4",
      placeholder: '{"source text": "target text"}',
    });
    this.importPanelElement = el("div", { class: "card", hidden: true }, [
      this.importMapping,
      el("button", { class: "btn btn-primary", type: "button", text: t("Strings", "Import"), onClick: () => this.importTargets() }),
    ]);
    return this.importPanelElement;
  }

  async transform() {
    const confirmed = await confirmDialog(this.ctx, {
      message: this.ctx.t("Strings", "Apply configured transform(s)?"),
    });
    if (confirmed) this.runJob("transform");
  }

  async importTargets() {
    try {
      const mapping = JSON.parse(this.importMapping.value || "{}");
      await this.ctx.bridge.import_mapping(mapping);
      this.importPanelElement.hidden = true;
      this.importMapping.value = "";
      await Promise.all([this.queue.loadPage(), this.queue.refreshProgress()]);
    } catch (err) {
      this.ctx.showError(err);
    }
  }

  async runJob(name) {
    await this.ctx.startJob(name);
  }

  // The chosen interface lives on the model screen; here it decides one thing
  // only -- whether Translate has anything to run against.
  async refreshTranslateAvailability() {
    const catalog = await this.ctx.bridge.provider_interfaces();
    const active = catalog.interfaces.find(
      (item) => item.id === catalog.active_interface_id,
    );
    this.translateButton.disabled = !active;
  }

  // Stays on the page and the unit the translator is looking at: unlike
  // reload, the run changing targets is no reason to start the queue over.
  scheduleLiveRefresh() {
    if (this.liveRefreshTimer !== null) return;
    this.liveRefreshTimer = setTimeout(async () => {
      this.liveRefreshTimer = null;
      try {
        await Promise.all([this.queue.loadPage(), this.queue.refreshProgress()]);
        this.refreshOpenUnitIfUntouched();
      } catch (err) {
        this.ctx.showError(err);
      }
    }, LIVE_REFRESH_MS);
  }

  // The open unit is read again only when the run gave it its first target:
  // an empty box is the one case where nothing the translator typed is lost.
  refreshOpenUnitIfUntouched() {
    const { editor } = this;
    if (editor.selection === null || editor.targetBox.value) return;
    const shown = this.queue.renderedRows.find(({ row }) => editor.isSelected(row));
    if (shown && shown.row.status !== editor.currentStatus) {
      editor.selectUnit(editor.selection.source_file, editor.selection.id).catch((err) => this.ctx.showError(err));
    }
  }

  onEvent(event) {
    const { editor, queue } = this;
    const showError = (err) => this.ctx.showError(err);
    const selection = editor.selection;
    if (event.type === "conflict" && selection?.source_file === event.file && selection.id === event.unit_id) {
      this.conflictOnSelection = true;
    } else if (JOB_ENDED.has(event.type) && MUTATING_JOBS.has(event.job)) {
      clearTimeout(this.liveRefreshTimer);
      this.liveRefreshTimer = null;
      queue.refreshLanguages().then(() => queue.reload()).catch(showError);
      queue.refreshProgress().catch(showError);
    } else if (
      event.type === "progress" &&
      event.job === "translate" &&
      event.message === "translate.batch.saved"
    ) {
      this.scheduleLiveRefresh();
    } else if (event.type === "strings-changed") {
      this.scheduleLiveRefresh();
      if (selection?.source_file === event.file && editor.targetBox.value === editor.savedTarget) {
        editor.selectUnit(selection.source_file, selection.id, true).catch(showError);
      }
    }
  }

  renderQualityCount() {
    const report = this.ctx.appState.qaReport;
    const defects = report?.issues.filter((issue) => issue.severity !== "warning").length ?? 0;
    this.toolbarQuality.textContent = report
      ? this.ctx.t("StringsScreen", "{count} defects", { count: defects })
      : "";
  }

  // Coming back to the screen, or being sent to a line, first saves the draft
  // left in the editor; one that will not save stays there with its error.
  // Once a conflict on that line is answered the project on disk decides, so
  // the draft is dropped, whichever screen was showing when it was answered.
  async refresh() {
    const { appState } = this.ctx;
    const { editor, queue } = this;
    this.renderQualityCount();
    const answered = this.conflictOnSelection && !this.ctx.conflictPending?.();
    if (answered) this.conflictOnSelection = false;
    const keepDraft = !answered && editor.selection !== null && !(await editor.saveDraft());
    const qaTarget = keepDraft ? null : (appState.pendingQaTarget ?? null);
    appState.pendingQaTarget = null;
    const pendingSearch = takePendingStringSearch(appState);
    if (qaTarget === null && pendingSearch !== null && !keepDraft) {
      queue.applySearch(pendingSearch.query, pendingSearch.scope);
      editor.clear();
    }
    await queue.refreshLanguages();
    await Promise.all([
      qaTarget === null ? queue.loadPage() : Promise.resolve(),
      queue.refreshProgress(),
      this.refreshTranslateAvailability(),
    ]);
    if (qaTarget !== null) {
      await queue.focusQaTarget(qaTarget);
    } else if (editor.selection !== null && !keepDraft) {
      await editor.selectUnit(editor.selection.source_file, editor.selection.id);
    }
  }

  dispose() {
    clearTimeout(this.liveRefreshTimer);
    this.unsubscribe();
    this.unsubscribeQuality();
    this.editorHost.close(true);
  }
}
