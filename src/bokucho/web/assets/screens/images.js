// images.js -- inventory, live preview (via `image_data`), the scan/export/
// collect job buttons, and mounting the canvas editor for pixel + typesetting
// edits.

import { el, clear, uniqueId } from "../dom.js";
import { choiceDialog, confirmDialog } from "../confirm.js";
import { fontOptionElements, listedFontOptions } from "../font_options.js";
import { createImageEditorHost } from "../image_editor_modal.js";
import { htmlLang } from "../language_select.js";
import { listTarget } from "../list_keys.js";
import { createVirtualList } from "../virtual_list.js";

// The runtime is two gigabytes and the checkpoints together are under half
// a gigabyte, so the size is said in the unit it is in rather than offering to
// download "0.17 GB".
function downloadSize(bytes) {
  return bytes >= 1_000_000_000
    ? `${(bytes / 1_000_000_000).toFixed(2)} GB`
    : `${(bytes / 1_000_000).toFixed(2)} MB`;
}

// Two lines of text per row, so the row follows the interface font size.
function imageRowHeight() {
  const scale = parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue("--font-scale"),
  );
  return Math.round(39 * (scale || 16 / 13));
}

// The pictures selected after a click on `clicked`: a plain click selects it
// alone, Ctrl adds or takes out one, and Shift takes the run from the last
// plain or Ctrl click, in the order the list shows them.
export function nextSelection(order, selected, anchor, clicked, { ctrl = false, shift = false } = {}) {
  if (shift && anchor !== null) {
    const from = order.indexOf(anchor);
    const to = order.indexOf(clicked);
    if (from !== -1 && to !== -1) {
      return { selected: order.slice(Math.min(from, to), Math.max(from, to) + 1), anchor };
    }
  }
  if (ctrl) {
    const next = selected.includes(clicked)
      ? selected.filter((image) => image !== clicked)
      : [...selected, clicked];
    return { selected: next, anchor: clicked };
  }
  return { selected: [clicked], anchor: clicked };
}

// What the ignore button does to the selected entries: ignore them while any
// is not, hand them back once every one is.
export function ignoreAction(entries) {
  if (!entries.length) return null;
  return entries.every((entry) => entry.ignored) ? "unignore" : "ignore";
}

// The detection threshold a picture the scan missed is looked at again with.
// One picture scored under the 0.9 it was scanned at; at 0.5 its box was found,
// and the reader, still held to its own threshold, read it at 0.999.
const LOWER_DETECTION_THRESHOLD = 0.5;

// What scanning the selected pictures again offers: the threshold the OCR
// settings name, and a lower one where the settings are not already under it.
export function rescanChoices(detectionThreshold, t) {
  const lower = detectionThreshold > LOWER_DETECTION_THRESHOLD;
  const choices = [
    { key: "cancel", label: t("Common", "Cancel") },
    {
      key: "settings",
      label: t("Images", "Use OCR settings (threshold {value})", {
        value: detectionThreshold,
      }),
      threshold: detectionThreshold,
      primary: !lower,
    },
  ];
  if (lower) {
    choices.push({
      key: "lower",
      label: t("Images", "Look for more text (threshold {value})", {
        value: LOWER_DETECTION_THRESHOLD,
      }),
      threshold: LOWER_DETECTION_THRESHOLD,
      primary: true,
    });
  }
  return choices;
}

// Whether a job just ended that may have rewritten the inventory. A scan writes
// down what it has up to a cancel or a failure too; refreshed only on success,
// the list went on showing 2,343 pictures over a file the stopped scan had left with 79.
export function inventoryMayHaveChanged(event) {
  return ["succeeded", "failed", "cancelled"].includes(event.type)
    && String(event.job ?? "").startsWith("images-");
}

// The faces the font field offers: the default, named for the face it stands
// for, then the shipped and installed ones (null while they are still being
// read), with the chosen face kept even when the list does not hold it.
export function fontOptions({ font, default: fallback }, installed, t) {
  return [
    {
      value: "",
      label: fallback?.name
        ? t("Images", "Default ({name})", { name: fallback.name })
        : t("Images", "Default"),
    },
    ...listedFontOptions(font, installed, t),
  ];
}

function inventoryCell(value, className = "") {
  const text = String(value ?? "");
  return el("div", {
    class: `image-inventory-cell ${className}`.trim(),
    text,
    title: text,
  });
}

function chipCell(className, text, title) {
  return el("div", { class: "image-inventory-cell" }, [el("span", { class: className, text, title })]);
}

function textCell(entry, ctx) {
  const { t } = ctx;
  if (entry.error) return chipCell("chip chip-error", entry.error, entry.error);
  if (entry.ignored) return chipCell("chip chip-quiet", t("Images", "Ignored"));
  const regions = Array.isArray(entry.regions) ? entry.regions : [];
  if (!regions.length) {
    // A picture the scan read carries a `boxes` key whether or not anything
    // was on it, so a blank column can say which blank it is: nothing to
    // translate, or nothing asked yet.
    if (!Array.isArray(entry.boxes)) {
      return el("div", {
        class: "image-inventory-cell image-inventory-quiet",
        text: t("Images", "Not scanned"),
      });
    }
    return chipCell("chip chip-quiet", t("Images", "No text"));
  }
  const read = regions.map((region) => region.text).join(" · ");
  return el("div", { class: "image-inventory-cell image-inventory-read" }, [
    el("span", { class: "chip", text: String(regions.length) }),
    el("span", {
      class: "image-inventory-read-text",
      lang: htmlLang(ctx.appState.project?.source_lang),
      text: read,
      title: read,
    }),
  ]);
}

function entryOrder(sortColumn, sortDirection, collator) {
  const area = (entry) => (entry.width ?? 0) * (entry.height ?? 0);
  return (left, right) => {
    let comparison = 0;
    if (sortColumn === "size") {
      comparison = area(left) - area(right)
        || (left.width ?? 0) - (right.width ?? 0)
        || (left.height ?? 0) - (right.height ?? 0);
    } else if (sortColumn === "text") {
      comparison = (left.regions?.length ?? 0) - (right.regions?.length ?? 0);
    }
    return sortDirection * (comparison
      || collator.compare(left.image.split("/").at(-1), right.image.split("/").at(-1))
      || collator.compare(left.image, right.image));
  };
}

// Every part the status reports, named as it names them: a field that is not
// there reads as undefined, and one undefined turns the whole sum into NaN --
// which is not greater than zero, so the dialog goes quiet and the download
// starts unasked.
function pendingBytes(modelStatus, parts) {
  return (modelStatus.runtime_cached ? 0 : modelStatus.runtime_download_bytes)
    + parts.reduce((sum, part) => sum + (modelStatus[part].cached ? 0 : modelStatus[part].download_bytes), 0);
}

export function mount(container, ctx) {
  const screen = new ImagesScreen(ctx);
  container.replaceChildren(screen.element);
  const refresh = () => screen.refresh();
  const unsubscribe = ctx.onEvent((event) => {
    if (inventoryMayHaveChanged(event)) {
      refresh().catch((err) => ctx.showError(err));
    }
  });

  refresh().catch((err) => ctx.showError(err));
  screen.loadFonts().catch((err) => ctx.showError(err));

  return {
    refresh,
    unmount() {
      unsubscribe();
      screen.editorHost.close(true);
    },
  };
}

class ImageInventory {
  constructor(ctx, { onSelectionChange, onPick }) {
    const { t } = ctx;
    this.ctx = ctx;
    this.onSelectionChange = onSelectionChange;
    this.onPick = onPick;
    this.collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
    this.sortColumn = "image";
    this.sortDirection = 1;
    this.entries = [];
    this.selected = [];
    this.anchor = null;
    this.activeImage = null;
    this.sortButtons = [
      ["image", t("Images", "Image")],
      ["size", t("Images", "Size")],
      ["text", t("Images", "Text found")],
    ].map(([column, label]) => this.sortButton(column, label));
    this.header = el("div", {
      class: "image-inventory-grid image-inventory-header",
    }, this.sortButtons.map(({ button }) => button));
    this.showSort();
    this.element = this.card();
    this.rowHeight = imageRowHeight();
    this.list = createVirtualList({
      viewport: this.viewport,
      rowHeight: this.rowHeight,
      loadSlice: (offset, limit) => this.entries.slice(offset, offset + limit),
      renderRow: (entry, index) => this.row(entry, index),
      onRender: () => this.keepATabStop(),
    });
    this.viewport.addEventListener("scroll", () => {
      this.header.style.transform = `translateX(${-this.viewport.scrollLeft}px)`;
    });
    this.viewport.addEventListener("keydown", (event) => this.onKeydown(event));
  }

  card() {
    const title = el("h2", {
      class: "card-title",
      id: uniqueId("image-inventory-title"),
      text: this.ctx.t("Images", "Image inventory"),
    });
    this.viewport = el("div", {
      class: "image-inventory-viewport",
      role: "listbox",
      "aria-multiselectable": "true",
      "aria-labelledby": title.id,
    });
    return el("div", { class: "card image-inventory" }, [
      title,
      el("div", { class: "image-inventory-header-viewport" }, [this.header]),
      this.viewport,
    ]);
  }

  sortButton(column, label) {
    const indicator = el("span", { "aria-hidden": "true" });
    const button = el("button", {
      class: "image-inventory-sort",
      type: "button",
      onclick: () => this.sortBy(column),
    }, [label, indicator]);
    return { column, label, button, indicator };
  }

  sortBy(column) {
    this.sortDirection = this.sortColumn === column ? -this.sortDirection : 1;
    this.sortColumn = column;
    this.sortEntries();
    this.showSort();
    this.viewport.scrollTop = 0;
    this.list.setTotal(this.entries.length);
  }

  showSort() {
    const { t } = this.ctx;
    for (const { column, label, button, indicator } of this.sortButtons) {
      const active = column === this.sortColumn;
      const direction = t("Images", this.sortDirection === 1 ? "Ascending" : "Descending");
      indicator.textContent = active ? (this.sortDirection === 1 ? "▲" : "▼") : "";
      button.setAttribute("aria-pressed", String(active));
      button.setAttribute("aria-label", active ? `${label} (${direction})` : label);
    }
  }

  sortEntries() {
    this.entries.sort(entryOrder(this.sortColumn, this.sortDirection, this.collator));
  }

  setEntries(entries) {
    this.entries = entries;
    this.sortEntries();
    if (!this.entries.some((entry) => entry.image === this.activeImage)) this.activeImage = null;
    this.list.setTotal(this.entries.length);
    this.showSelection();
  }

  find(image) {
    return this.entries.find((row) => row.image === image);
  }

  selectedEntries() {
    return this.entries.filter((row) => this.selected.includes(row.image));
  }

  rescannable() {
    return this.selectedEntries().filter((row) => !row.ignored).map((row) => row.image);
  }

  row(entry, index) {
    const parts = String(entry.image ?? "").split("/");
    const name = parts.pop() ?? "";
    const folder = parts.join("/");
    const chosen = this.selected.includes(entry.image);
    const row = el("div", {
      class: "image-inventory-grid image-inventory-row",
      role: "option",
      tabindex: entry.image === this.tabStopImage() ? "0" : "-1",
      "aria-selected": String(chosen),
      "aria-setsize": String(this.entries.length),
      "aria-posinset": String(index + 1),
    }, [
      el("div", { class: "image-inventory-cell image-inventory-name" }, [
        el("span", { class: "image-inventory-file", text: name, title: entry.image }),
        folder
          ? el("span", { class: "image-inventory-folder", text: folder, title: folder })
          : null,
      ]),
      inventoryCell(`${entry.width ?? "--"} × ${entry.height ?? "--"}`, "image-inventory-quiet"),
      textCell(entry, this.ctx),
    ]);
    row.classList.toggle("selected", chosen);
    // Shift picks a run of rows here, not a run of text across them.
    row.addEventListener("mousedown", (event) => {
      if (event.shiftKey) event.preventDefault();
    });
    row.addEventListener("click", (event) => this.click(entry, event));
    return row;
  }

  // The row Tab lands on is the one last used, or the first until one is.
  tabStopImage() {
    return this.activeImage ?? this.entries[0]?.image;
  }

  setActiveImage(image) {
    this.activeImage = image;
    this.list.updateRendered((node, row) => {
      node.tabIndex = row.image === image ? 0 : -1;
    });
  }

  // Only the rows near the viewport exist, so the list could scroll away from
  // its tab stop; the first row that is drawn takes it over then.
  keepATabStop() {
    let first = null;
    let present = false;
    this.list.updateRendered((node, row) => {
      first ??= row;
      present ||= node.tabIndex === 0;
    });
    if (!present && first) this.setActiveImage(first.image);
  }

  // Arrows move and select like a click, Shift extends the run, Ctrl moves
  // without selecting, and Space toggles the focused picture like Ctrl-click.
  onKeydown(event) {
    const entries = this.entries;
    if (event.altKey || event.metaKey || !entries.length) return;
    const current = Math.max(0, entries.findIndex((entry) => entry.image === this.tabStopImage()));
    if (event.key === " ") {
      event.preventDefault();
      this.click(entries[current], { ctrlKey: true });
      return;
    }
    const pageRows = Math.max(1, Math.floor(this.viewport.clientHeight / this.rowHeight) - 1);
    const to = listTarget(event.key, current, entries.length, pageRows);
    if (to === null) return;
    event.preventDefault();
    this.setActiveImage(entries[to].image);
    this.list.focusIndex(to);
    if (!event.ctrlKey) this.click(entries[to], { shiftKey: event.shiftKey });
  }

  click(entry, event) {
    this.setActiveImage(entry.image);
    ({ selected: this.selected, anchor: this.anchor } = nextSelection(
      this.entries.map((row) => row.image),
      this.selected,
      this.anchor,
      entry.image,
      { ctrl: event.ctrlKey || event.metaKey, shift: event.shiftKey },
    ));
    this.showSelection();
    if (this.selected.includes(entry.image)) this.onPick(entry);
  }

  showSelection() {
    this.list.updateRendered((node, row) => {
      const chosen = this.selected.includes(row.image);
      node.classList.toggle("selected", chosen);
      node.setAttribute("aria-selected", String(chosen));
    });
    this.onSelectionChange();
  }
}

class ImagePreview {
  constructor(ctx, onOpen) {
    const { t } = ctx;
    this.ctx = ctx;
    this.entry = null;
    this.request = 0;
    this.image = el("img", { id: "image-preview", hidden: true });
    this.regions = el("div", { class: "image-preview-regions" });
    this.hint = el("p", { class: "muted", text: t("Images", "Select an image to preview and edit.") });
    this.meta = el("p", { class: "muted" });
    this.openButton = el("button", {
      class: "btn btn-primary",
      type: "button",
      text: t("Images", "Open editor"),
      disabled: true,
      onClick: () => {
        if (this.entry) onOpen(this.entry);
      },
    });
    this.image.addEventListener("error", () =>
      this.unavailable(t("Images", "Preview unavailable: this image format cannot be shown.")));
    this.element = el("div", { class: "card image-preview-card" }, [
      el("h2", { class: "card-title", text: t("Images", "Live preview") }),
      el("div", { class: "image-preview-frame" }, [this.image, this.regions]),
      this.hint,
      this.meta,
      this.openButton,
    ]);
  }

  unavailable(message) {
    this.image.hidden = true;
    clear(this.regions);
    this.hint.hidden = false;
    this.hint.textContent = message;
  }

  // The preview shows the picture clicked last; the selection may hold more.
  async show(entry) {
    const { bridge, t } = this.ctx;
    this.entry = entry;
    this.openButton.disabled = false;
    this.meta.textContent = t("Images", "{width} x {height} / {mode} / {format}\n{path}", {
      width: entry.width ?? "--",
      height: entry.height ?? "--",
      mode: entry.mode ?? "--",
      format: entry.format ?? "--",
      path: entry.image ?? "",
    });
    this.renderRegions(entry);
    const request = ++this.request;
    try {
      const result = await bridge.image_data(entry.image);
      if (request !== this.request) return;
      this.image.src = `data:image/png;base64,${result.data}`;
      this.image.hidden = false;
      this.hint.hidden = true;
    } catch (err) {
      if (request !== this.request) return;
      this.unavailable(t("Images", "Preview unavailable: {error}", { error: err.message }));
    }
  }

  // The boxes are percentages of the picture, so they stay on the text at
  // whatever size the preview happens to be drawn.
  renderRegions(entry) {
    clear(this.regions);
    if (!entry.width || !entry.height) return;
    for (const region of entry.regions ?? []) {
      const [x0, y0, x1, y1] = region.box;
      this.regions.append(el("div", {
        class: "image-preview-region",
        title: region.text,
        style: `left:${(x0 / entry.width) * 100}%;`
          + `top:${(y0 / entry.height) * 100}%;`
          + `width:${((x1 - x0) / entry.width) * 100}%;`
          + `height:${((y1 - y0) / entry.height) * 100}%`,
      }));
    }
  }
}

class ImagesScreen {
  constructor(ctx) {
    this.ctx = ctx;
    this.typesetFont = { font: null, default: null };
    this.installedFonts = null;
    this.status = el("p", { class: "muted images-status", hidden: true });
    const toolbar = this.toolbar();
    this.inventory = new ImageInventory(ctx, {
      onSelectionChange: () => this.showSelectionActions(),
      onPick: (entry) => this.preview.show(entry),
    });
    this.preview = new ImagePreview(ctx, (entry) => this.openEditor(entry));
    this.element = el("div", { class: "images-screen" }, [
      toolbar,
      this.status,
      el("div", { class: "images-workspace" }, [this.inventory.element, this.preview.element]),
    ]);
    this.editorHost = createImageEditorHost(ctx);
  }

  toolbar() {
    const { t } = this.ctx;
    const button = (text, onClick, extra = {}) =>
      el("button", { class: "btn btn-sm", type: "button", text, ...extra, onClick });
    this.ignoreButton = button(t("Images", "Ignore"), () => this.toggleIgnored(), { disabled: true });
    this.rescanButton = button(t("Images", "Scan selected again"), () => this.rescanSelected(), { disabled: true });
    this.fontSelect = el("select", {
      class: "images-font-select",
      "aria-label": t("Images", "Typeset font"),
    });
    this.fontSelect.addEventListener("change", () => this.saveFont());
    return el("div", { class: "images-toolbar" }, [
      this.ignoreButton,
      this.rescanButton,
      el("label", { class: "images-font" }, [
        el("span", { class: "muted", text: t("Images", "Typeset font") }),
        this.fontSelect,
      ]),
      button(t("Images", "Erase and typeset"), () => this.eraseAndTypeset()),
      button(t("Images", "Extract images"), () => this.ctx.startJob("images-extract")),
      // The scan only inventories files; the text units the translation pipeline
      // works on come from this pass, so it is the primary action here.
      button(t("Images", "Scan and read text"), () => this.scan(), { class: "btn btn-sm btn-primary" }),
    ]);
  }

  async refresh() {
    const result = await this.ctx.bridge.image_inventory();
    this.inventory.setEntries(result.entries);
  }

  showSelectionActions() {
    const { t } = this.ctx;
    const action = ignoreAction(this.inventory.selectedEntries());
    this.ignoreButton.disabled = action === null;
    // An ignored picture is left as it is, so it is nothing to scan again.
    this.rescanButton.disabled = !this.inventory.rescannable().length;
    this.ignoreButton.textContent = action === "unignore"
      ? t("Images", "Unignore")
      : t("Images", "Ignore");
  }

  showStatus(text) {
    this.status.hidden = false;
    this.status.textContent = text;
  }

  showFonts() {
    const { t } = this.ctx;
    const options = fontOptionElements(fontOptions(this.typesetFont, this.installedFonts, t));
    if (this.installedFonts === null) {
      options.push(el("option", { disabled: true, text: t("Images", "Loading fonts…") }));
    }
    this.fontSelect.replaceChildren(...options);
    this.fontSelect.value = this.typesetFont.font ? this.typesetFont.font.path : "";
  }

  async loadFonts() {
    const { bridge } = this.ctx;
    this.typesetFont = await bridge.typeset_font();
    this.showFonts();
    // Reading every installed face takes seconds; the field works meanwhile.
    this.installedFonts = (await bridge.typeset_fonts()).fonts;
    this.showFonts();
  }

  async saveFont() {
    this.fontSelect.disabled = true;
    try {
      this.typesetFont = await this.ctx.bridge.save_typeset_font(this.fontSelect.value || null);
    } catch (err) {
      this.ctx.showError(err);
    } finally {
      this.fontSelect.disabled = false;
      this.showFonts();
    }
  }

  // The two OCR halves and the runtime they run in, downloaded once; asked
  // about before a scan of any size starts one.
  async ocrDownloadApproved() {
    const { t } = this.ctx;
    const modelStatus = await this.ctx.bridge.ocr_status();
    const bytes = pendingBytes(modelStatus, ["detector", "recognizer"]);
    if (bytes <= 0) return { approved: true, modelStatus };
    const approved = await confirmDialog(this.ctx, {
      message: t("Images", "OCR needs a one-time {size} download before this scan. Continue?", {
        size: downloadSize(bytes),
      }),
      confirmText: t("Images", "Download and scan"),
    });
    return { approved, modelStatus };
  }

  async rescanSelected() {
    const { t } = this.ctx;
    const images = this.inventory.rescannable();
    if (!images.length) return;
    try {
      const { approved, modelStatus } = await this.ocrDownloadApproved();
      if (!approved) return;
      const choices = rescanChoices(modelStatus.detection_threshold, t);
      const answer = await choiceDialog({
        message: images.length === 1
          ? t("Images", "Scan the selected image again?")
          : t("Images", "Scan the {count} selected images again?", { count: images.length }),
        choices,
      });
      const choice = choices.find((entry) => entry.key === answer);
      if (!choice || choice.threshold === undefined) return;
      const started = await this.ctx.startJob("images-ocr-selected", {
        images,
        detection_threshold: choice.threshold,
      });
      if (started) this.showStatus(t("Images", "Scanning the selected images again."));
    } catch (err) {
      this.ctx.showError(err);
    }
  }

  async toggleIgnored() {
    const action = ignoreAction(this.inventory.selectedEntries());
    if (action === null) return;
    try {
      await this.ctx.bridge.set_images_ignored(this.inventory.selected, action === "ignore");
      await this.refresh();
    } catch (err) {
      this.ctx.showError(err);
    }
  }

  openEditor({ image }) {
    // A box moved in the editor changes what the inventory says was read, so
    // the entry is re-read rather than redrawn from what it was opened with.
    this.editorHost.open(image, {
      onSaved: () =>
        this.refresh()
          .then(() => {
            const entry = this.inventory.find(image);
            if (entry) this.preview.show(entry);
          })
          .catch((err) => this.ctx.showError(err)),
    });
  }

  // Reading a whole game is hours of GPU time, so a scan that already ran --
  // to the end, or up to a cancel -- is asked about before it is thrown away.
  async scanJob() {
    const { t } = this.ctx;
    if (!this.inventory.entries.length) return "images-ocr";
    const answer = await choiceDialog({
      message: t(
        "Images",
        "This project already holds a scan. Carry on from where it stopped, or scan every image again?",
      ),
      choices: [
        { key: "cancel", label: t("Common", "Cancel") },
        { key: "rescan", label: t("Images", "Scan again") },
        { key: "resume", label: t("Images", "Carry on"), primary: true },
      ],
    });
    if (answer === "rescan") return "images-ocr-rescan";
    return answer === "resume" ? "images-ocr" : null;
  }

  async scan() {
    const { t } = this.ctx;
    const job = await this.scanJob();
    if (job === null) return;
    if (!(await this.ocrDownloadApproved()).approved) return;
    if (!(await this.ctx.startJob(job))) return;
    this.showStatus(
      job === "images-ocr-rescan" || !this.inventory.entries.length
        ? t("Images", "Reading text from every image — this takes a while.")
        : t("Images", "Carrying on from where the last scan stopped."),
    );
  }

  async eraseAndTypeset() {
    const { t } = this.ctx;
    // Rendering wants the runtime and the eraser, never the two OCR halves.
    const bytes = pendingBytes(await this.ctx.bridge.ocr_status(), ["eraser"]);
    if (bytes > 0) {
      const approved = await confirmDialog(this.ctx, {
        message: t("Images", "Rendering needs a one-time {size} download before this run. Continue?", {
          size: downloadSize(bytes),
        }),
        confirmText: t("Images", "Download and render"),
      });
      if (!approved) return;
    }
    if (!(await this.ctx.startJob("images-render"))) return;
    this.showStatus(t(
      "Images",
      "Erasing and typesetting images with text boxes. Images edited by hand in the editor are kept as they are.",
    ));
  }
}
