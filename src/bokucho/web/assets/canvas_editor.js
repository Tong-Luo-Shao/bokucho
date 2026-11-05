// GraphicsGale-style layered pixel editor for the Images workspace.

import { el, labelControl, uniqueId } from "./dom.js";
import {
  canvasBase64,
  canvasFromBase64,
  clearRegion,
  cloneCanvas,
  compositeFrame,
  copyRegion,
  drawBrush,
  drawEllipse,
  drawFrame,
  drawLine,
  drawRectangle,
  floodFill,
  hexFromRgba,
  makeCanvas,
  normalizeRect,
  pasteRegion,
  pixelAt,
  replaceColor,
  rgbaFromHex,
} from "./image_editor/canvas.js";
import { loadFace } from "./font_faces.js";
import { fontOptionElements, listedFontOptions } from "./font_options.js";
import { EditorHistory } from "./image_editor/history.js";
import { icon } from "./image_editor/icons.js";

const ZOOM_LEVELS = [0.125, 0.25, 0.5, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32];
const DEFAULT_PALETTE = [
  "#000000ff",
  "#1f2937ff",
  "#475569ff",
  "#94a3b8ff",
  "#e2e8f0ff",
  "#ffffffff",
  "#7f1d1dff",
  "#dc2626ff",
  "#fb923cff",
  "#facc15ff",
  "#365314ff",
  "#65a30dff",
  "#14b8a6ff",
  "#0e7490ff",
  "#1d4ed8ff",
  "#60a5faff",
  "#312e81ff",
  "#7e22ceff",
  "#c026d3ff",
  "#f472b6ff",
  "#78350fff",
  "#a16207ff",
  "#d6d3d1ff",
  "#64748bff",
  "#00000000",
];

export function paletteForImageMode(mode, sourcePalette) {
  if (Array.isArray(sourcePalette) && sourcePalette.length > 0) {
    return [...sourcePalette];
  }
  if (mode === "1") return ["#000000ff", "#ffffffff"];
  if (mode === "L" || mode === "LA") {
    const grayscale = Array.from({ length: 17 }, (_, index) => {
      const channel = Math.round((index * 255) / 16)
        .toString(16)
        .padStart(2, "0");
      return `#${channel}${channel}${channel}ff`;
    });
    return mode === "LA" ? [...grayscale, "#00000000"] : grayscale;
  }
  if (mode === "P") return ["#000000ff", "#ffffffff"];
  return [...DEFAULT_PALETTE];
}

export function insertFrame(model, frame) {
  model.frames.splice(model.activeFrame + 1, 0, frame);
  model.activeFrame += 1;
  if (model.outputFrame >= model.activeFrame) model.outputFrame += 1;
}

export function pointerToolForButton(selectedTool, button) {
  return button === 1 ? "hand" : selectedTool;
}

const TOOLS = [
  { id: "pencil", label: "Pencil", shortcut: "B", group: "paint" },
  { id: "eraser", label: "Eraser", shortcut: "E", group: "paint" },
  { id: "fill", label: "Fill", shortcut: "G", group: "paint" },
  { id: "eyedropper", label: "Eyedropper", shortcut: "I", group: "paint" },
  { id: "replace", label: "Color replacer", shortcut: "C", group: "paint" },
  { id: "line", label: "Line", shortcut: "L", group: "shape" },
  { id: "rectangle", label: "Rectangle", shortcut: "R", group: "shape" },
  { id: "ellipse", label: "Ellipse", shortcut: "O", group: "shape" },
  { id: "select", label: "Rectangular selection", shortcut: "M", group: "region" },
  { id: "text", label: "Text", shortcut: "T", group: "region" },
  { id: "bbox", label: "OCR box", shortcut: "K", group: "region" },
  { id: "hand", label: "Pan", shortcut: "H", group: "view" },
];
const BRUSH_TOOLS = new Set(["pencil", "eraser", "line", "rectangle", "ellipse"]);
const SHAPE_TOOLS = new Set(["rectangle", "ellipse"]);
const TEXT_TOOLS = new Set(["text", "bbox"]);

export function bboxGestureAt(regions, point, handle, selected = null) {
  for (let index = regions.length - 1; index >= 0; index -= 1) {
    const [x0, y0, x1, y1] = regions[index].box;
    const inside = point.x >= x0 && point.y >= y0 && point.x < x1 && point.y < y1;
    // The chosen box's grips straddle its border, so their outer half belongs
    // to it too; any other box ends at its own edge.
    const reach = index === selected ? handle : 0;
    const near =
      point.x >= x0 - reach && point.y >= y0 - reach && point.x < x1 + reach && point.y < y1 + reach;
    if (!inside && !near) continue;
    // A side is a grip only where the box has room for one inside; a box
    // narrower than two grips drags whole and is pulled from outside.
    const gripX = x1 - x0 > handle * 2 ? handle : 0;
    const gripY = y1 - y0 > handle * 2 ? handle : 0;
    const edges = {
      left: point.x < x0 + gripX,
      top: point.y < y0 + gripY,
      right: point.x >= x1 - gripX,
      bottom: point.y >= y1 - gripY,
    };
    if (edges.left || edges.top || edges.right || edges.bottom) {
      return { kind: "resize", index, edges };
    }
    return { kind: "move", index };
  }
  return { kind: "create", index: -1 };
}

export function bboxCursor(gesture) {
  if (gesture.kind !== "resize") return gesture.kind;
  const { left, top, right, bottom } = gesture.edges;
  if ((left && top) || (right && bottom)) return "nwse";
  if ((right && top) || (left && bottom)) return "nesw";
  return left || right ? "ew" : "ns";
}

export function resizeBox([x0, y0, x1, y1], edges, dx, dy, width, height) {
  return [
    edges.left ? Math.max(0, Math.min(x1 - 1, x0 + dx)) : x0,
    edges.top ? Math.max(0, Math.min(y1 - 1, y0 + dy)) : y0,
    edges.right ? Math.min(width, Math.max(x0 + 1, x1 + dx)) : x1,
    edges.bottom ? Math.min(height, Math.max(y0 + 1, y1 + dy)) : y1,
  ];
}

export function moveBox([x0, y0, x1, y1], dx, dy, width, height) {
  const stepX = Math.max(-x0, Math.min(width - x1, dx));
  const stepY = Math.max(-y0, Math.min(height - y1, dy));
  return [x0 + stepX, y0 + stepY, x1 + stepX, y1 + stepY];
}

function button(text, title, className = "btn") {
  return el("button", { class: className, type: "button", text, title });
}

function iconButton(name, label, className = "editor-icon-button", shortcut = "") {
  const node = el("button", {
    class: className,
    type: "button",
    title: shortcut ? `${label} (${shortcut})` : label,
    "aria-label": label,
  });
  node.append(icon(name));
  return node;
}

function setIcon(node, name) {
  node.querySelector(".editor-icon")?.replaceWith(icon(name));
}

function field(label, input, compact = false) {
  return el("div", { class: compact ? "editor-field editor-field-compact" : "editor-field" }, [
    labelControl(el("label", { text: label }), input),
    input,
  ]);
}

function argbToCss(hex) {
  const clean = String(hex || "").replace("#", "");
  if (clean.length !== 8) return "transparent";
  return `#${clean.slice(2)}${clean.slice(0, 2)}`;
}

function cloneValue(value) {
  return JSON.parse(JSON.stringify(value));
}

function paletteColor(value) {
  const clean = String(value || "").replace("#", "").padEnd(8, "f").slice(0, 8);
  return {
    hex: `#${clean.slice(0, 6)}`,
    alpha: Number.parseInt(clean.slice(6), 16),
  };
}

function nextIdentifier(prefix, identifiers) {
  let sequence = 1;
  while (identifiers.has(`${prefix}-${sequence}`)) sequence += 1;
  return `${prefix}-${sequence}`;
}

async function hydrateEditorState(payload) {
  const frames = await Promise.all(
    payload.frames.map(async (frame) => ({
      id: frame.id,
      duration_ms: frame.duration_ms,
      layers: await Promise.all(
        frame.layers.map(async (layer) => ({
          id: layer.id,
          name: layer.name,
          visible: layer.visible,
          opacity: layer.opacity,
          locked: layer.locked,
          canvas: await canvasFromBase64(payload.width, payload.height, layer.data),
        })),
      ),
    })),
  );
  const textLayer = cloneValue(payload.text_layer);
  textLayer.regions = Array.isArray(textLayer.regions) ? textLayer.regions : [];
  return {
    path: payload.path,
    width: payload.width,
    height: payload.height,
    mode: payload.mode,
    format: payload.format,
    hasAlpha: payload.has_alpha,
    editable: payload.editable,
    sourceFingerprint: payload.source_fingerprint,
    outputFingerprint: payload.output_fingerprint,
    palette: paletteForImageMode(payload.mode, payload.palette),
    paletteLocked: ["1", "L", "LA", "P"].includes(payload.mode),
    alphaLocked: !payload.has_alpha || payload.mode === "P",
    frames,
    activeFrame: payload.active_frame,
    outputFrame: payload.output_frame,
    activeLayer: Math.max(0, frames[payload.active_frame].layers.length - 1),
    onionSkin: cloneValue(payload.onion_skin),
    textLayer,
    selectedRegion: null,
    selection: null,
    tool: "pencil",
    zoomIndex: ZOOM_LEVELS.indexOf(1),
    grid: true,
    brushSize: 1,
    filledShapes: false,
  };
}

const NUDGE_KEYS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
const GRIPS = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];

function iconAction(name, label, className, run, shortcut = "") {
  const node = iconButton(name, label, className, shortcut);
  node.addEventListener("click", run);
  return node;
}

function placeRect(node, x, y, width, height, zoom) {
  node.style.left = `${x * zoom}px`;
  node.style.top = `${y * zoom}px`;
  node.style.width = `${width * zoom}px`;
  node.style.height = `${height * zoom}px`;
}

function regionNode(region, index, model, zoom) {
  const [x0, y0, x1, y1] = region.box;
  const selected = index === model.selectedRegion;
  const node = el(
    "div",
    { class: `editor-region${selected ? " is-selected" : ""}`, title: region.text },
    [el("span", { class: "editor-region-index", text: String(index + 1) })],
  );
  if (selected && model.tool === "bbox") {
    for (const grip of GRIPS) node.append(el("span", { class: "editor-region-grip", "data-grip": grip }));
  }
  placeRect(node, x0, y0, x1 - x0, y1 - y0, zoom);
  return node;
}

function layerThumbnail(layer) {
  const image = el("img", { class: "editor-layer-thumbnail", alt: "" });
  image.src = layer.canvas.toDataURL("image/png");
  return image;
}

function frameThumbnail(frame, model) {
  const image = el("img", { class: "editor-frame-thumbnail", alt: "" });
  image.src = compositeFrame(frame, model.width, model.height).toDataURL("image/png");
  return image;
}

function paletteNotice(model, tr) {
  if (model.mode === "1") return tr("Colors are constrained to black and white for this image mode.");
  if (model.mode === "L" || model.mode === "LA") {
    return tr("Colors are constrained to grayscale for this image mode.");
  }
  if (model.paletteLocked) return tr("Colors are constrained to the source palette for this image mode.");
  return tr("Left-click selects the primary color; right-click selects the secondary color.");
}

function readInteger(input, fallback) {
  const value = Math.round(Number(input.value));
  return Number.isFinite(value) && input.value !== "" ? value : fallback;
}

function serializeState(model) {
  const output = compositeFrame(model.frames[model.outputFrame], model.width, model.height);
  const outputData = canvasBase64(output);
  return {
    path: model.path,
    width: model.width,
    height: model.height,
    mode: model.mode,
    format: model.format,
    active_frame: model.activeFrame,
    output_frame: model.outputFrame,
    onion_skin: cloneValue(model.onionSkin),
    text_layer: cloneValue(model.textLayer),
    frames: model.frames.map((frame) => ({
      id: frame.id,
      duration_ms: frame.duration_ms,
      layers: frame.layers.map((layer) => ({
        id: layer.id,
        name: layer.name,
        data: canvasBase64(layer.canvas),
        visible: layer.visible,
        opacity: layer.opacity,
        locked: layer.locked,
      })),
    })),
    output_data: outputData,
    base_data: outputData,
  };
}

export async function mountEditor(container, path, bridge, options = {}) {
  const t = options.t ?? ((_context, source) => source);
  const tr = (source, params = {}) => t("ImageEditor", source, params);
  const loading = el("div", { class: "editor-loading", text: tr("Loading image editor...") });
  container.replaceChildren(loading);

  let model;
  try {
    model = await hydrateEditorState(await bridge.image_editor_state(path));
  } catch (error) {
    loading.className = "editor-load-error";
    loading.textContent = tr("Image editor failed to load: {error}", { error: error.message });
    return {
      destroy() {},
      isDirty: () => false,
      save: async () => false,
    };
  }

  const editor = new ImageEditor(model, { path, bridge, options, t, tr });
  container.replaceChildren(editor.root);
  editor.start();
  return {
    isDirty: () => editor.dirty,
    save: () => editor.save(),
    destroy: () => editor.destroy(),
  };
}

class ImageEditor {
  constructor(model, { path, bridge, options, t, tr }) {
    this.model = model;
    this.bridge = bridge;
    this.options = options;
    this.t = t;
    this.tr = tr;
    this.history = new EditorHistory();
    this.dirty = false;
    this.edits = 0;
    this.destroyed = false;
    this.clipboard = null;
    this.saving = false;
    const topbar = this.topbar(path);
    this.colors = new ColorControls(this);
    this.view = new CanvasView(this);
    const toolRail = this.toolRail();
    const optionsBar = this.optionsBar();
    const palettePanel = this.colors.panel();
    this.layers = new LayersPanel(this);
    this.text = new TextPanel(this);
    const inspector = this.inspector(palettePanel);
    this.timeline = new Timeline(this);
    this.pointer = new CanvasPointer(this);
    this.root = el("div", { class: "image-editor", tabindex: "0" }, [
      topbar,
      optionsBar,
      el("div", { class: "editor-main" }, [toolRail, this.view.area, inspector]),
      this.timeline.element,
    ]);
    this.root.addEventListener("keydown", (event) => this.onKeydown(event));
  }

  get regions() {
    return this.model.textLayer.regions;
  }

  topbar(path) {
    const { model, tr } = this;
    this.dirtyBadge = el("span", { class: "editor-dirty-badge", text: tr("Saved") });
    this.saveButton = button(tr("Save edit"), "Ctrl+S", "btn btn-primary");
    this.saveCloseButton = button(tr("Save and close"), tr("Save and close"));
    this.saveButton.addEventListener("click", () => this.save());
    this.saveCloseButton.addEventListener("click", () => this.saveAndClose());
    return el("header", { class: "editor-topbar" }, [
      el("div", { class: "editor-heading" }, [
        el("strong", {
          class: "editor-title",
          text: tr("Image editor - {name}", { name: path.split(/[\\/]/).at(-1) }),
        }),
        el("span", {
          class: "editor-document-meta",
          text: `${model.width} × ${model.height} · ${model.mode} · ${model.format ?? "--"}`,
        }),
        this.dirtyBadge,
      ]),
      el("div", { class: "editor-top-actions" }, [this.saveButton, this.saveCloseButton]),
    ]);
  }

  toolRail() {
    const { tr } = this;
    this.toolButtons = new Map();
    const rail = el("aside", { class: "editor-tool-rail", "aria-label": tr("Drawing tools") });
    let group = TOOLS[0].group;
    for (const tool of TOOLS) {
      if (tool.group !== group) {
        rail.append(el("span", { class: "editor-rail-separator", "aria-hidden": "true" }));
        group = tool.group;
      }
      const toolButton = iconAction(
        tool.id,
        tr(tool.label),
        "editor-tool-button",
        () => this.setTool(tool.id),
        tool.shortcut,
      );
      toolButton.dataset.tool = tool.id;
      this.toolButtons.set(tool.id, toolButton);
      rail.append(toolButton);
    }
    rail.append(this.colors.pair());
    return rail;
  }

  optionsBar() {
    const { tr } = this;
    this.undoButton = iconAction("undo", tr("Undo"), "editor-icon-button", () => this.undo(), "Ctrl+Z");
    this.redoButton = iconAction("redo", tr("Redo"), "editor-icon-button", () => this.redo(), "Ctrl+Y");
    this.toolName = el("span", { class: "editor-tool-name" });
    this.brushOptions();
    return el("div", { class: "editor-options" }, [
      el("div", { class: "editor-option-group" }, [this.undoButton, this.redoButton]),
      el("span", { class: "editor-separator" }),
      this.toolName,
      this.brushField,
      this.filledField,
      this.selectionActions(),
      el("span", { class: "editor-toolbar-spacer" }),
      this.zoomControls(),
    ]);
  }

  brushOptions() {
    const { model, tr } = this;
    const brushSizeInput = el("input", { type: "number", min: "1", max: "32", value: "1" });
    const filledShapesInput = el("input", { type: "checkbox" });
    brushSizeInput.addEventListener("change", () => {
      model.brushSize = Math.max(1, Math.min(32, Number(brushSizeInput.value) || 1));
      brushSizeInput.value = String(model.brushSize);
    });
    filledShapesInput.addEventListener("change", () => {
      model.filledShapes = filledShapesInput.checked;
    });
    this.brushField = field(tr("Brush"), brushSizeInput, true);
    this.filledField = el("label", { class: "editor-check" }, [filledShapesInput, tr("Filled")]);
  }

  selectionActions() {
    const { tr } = this;
    this.copyButton = iconAction("copy", tr("Copy"), "editor-icon-button", () => this.copySelection(), "Ctrl+C");
    this.cutButton = iconAction("cut", tr("Cut"), "editor-icon-button", () => this.cutSelection(), "Ctrl+X");
    this.pasteButton = iconAction("paste", tr("Paste"), "editor-icon-button", () => this.pasteSelection(), "Ctrl+V");
    this.clearButton = iconAction(
      "trash",
      tr("Delete selection"),
      "editor-icon-button",
      () => this.deleteSelection(),
      "Delete",
    );
    this.selectionGroup = el("div", { class: "editor-option-group" }, [
      this.copyButton,
      this.cutButton,
      this.pasteButton,
      this.clearButton,
    ]);
    return this.selectionGroup;
  }

  zoomControls() {
    const { model, tr, view } = this;
    const gridButton = iconAction("grid", tr("Pixel grid"), "editor-icon-button", () => {
      model.grid = !model.grid;
      gridButton.setAttribute("aria-pressed", String(model.grid));
      view.applyZoom();
    });
    gridButton.setAttribute("aria-pressed", String(model.grid));
    return el("div", { class: "editor-option-group" }, [
      gridButton,
      el("span", { class: "editor-separator" }),
      iconAction("zoomOut", tr("Zoom out"), "editor-icon-button", () => view.zoomOut(), "-"),
      view.zoomLabel,
      iconAction("zoomIn", tr("Zoom in"), "editor-icon-button", () => view.zoomIn(), "+"),
      iconAction("fit", tr("Fit"), "editor-icon-button", () => view.fitZoom()),
    ]);
  }

  inspector(palettePanel) {
    const { tr } = this;
    const drawPanel = el(
      "div",
      { class: "editor-tab-panel", role: "tabpanel", id: uniqueId("editor-draw") },
      [this.view.previewPanel, palettePanel, this.layers.element],
    );
    const textPanel = el(
      "div",
      { class: "editor-tab-panel", role: "tabpanel", id: uniqueId("editor-text") },
      this.text.sections,
    );
    this.inspectorTabs = [
      { key: "draw", panel: drawPanel, label: [tr("Draw")] },
      { key: "text", panel: textPanel, label: [tr("Text"), this.text.count] },
    ].map((tab) => ({
      ...tab,
      button: el(
        "button",
        {
          class: "dock-tab",
          type: "button",
          role: "tab",
          "aria-controls": tab.panel.id,
          onClick: () => this.showInspectorTab(tab.key),
        },
        tab.label,
      ),
    }));
    return el("aside", { class: "editor-inspector" }, [
      el("div", { class: "dock-tabs editor-tabs", role: "tablist" }, this.inspectorTabs.map((tab) => tab.button)),
      drawPanel,
      textPanel,
    ]);
  }

  start() {
    const { model, tr } = this;
    this.text.syncInputs();
    this.colors.start();
    this.showInspectorTab(this.regions.length > 0 || model.textLayer.target ? "text" : "draw");
    this.timeline.show(model.frames.length > 1);
    this.timeline.showPlaying(false);
    this.setTool("pencil");
    this.renderAll();
    window.requestAnimationFrame(() => this.view.fitZoom());
    this.root.focus({ preventScroll: true });
    this.text.loadFontChoices().catch((error) =>
      this.setStatus(tr("Fonts failed to load: {error}", { error: error.message })),
    );
  }

  destroy() {
    this.destroyed = true;
    this.timeline.cancelTimer();
  }

  currentFrame() {
    return this.model.frames[this.model.activeFrame];
  }

  currentLayer() {
    const frame = this.currentFrame();
    this.model.activeLayer = Math.max(0, Math.min(this.model.activeLayer, frame.layers.length - 1));
    return frame.layers[this.model.activeLayer];
  }

  setStatus(message) {
    this.view.actionStatus.textContent = message;
  }

  markDirty() {
    this.dirty = true;
    this.edits += 1;
    this.dirtyBadge.textContent = this.tr("Unsaved image");
    this.dirtyBadge.classList.add("is-dirty");
    this.options.onDirtyChange?.(true);
  }

  markSaved() {
    this.dirty = false;
    this.dirtyBadge.textContent = this.tr("Saved");
    this.dirtyBadge.classList.remove("is-dirty");
    this.options.onDirtyChange?.(false);
  }

  updateHistoryButtons() {
    this.undoButton.disabled = !this.history.canUndo;
    this.redoButton.disabled = !this.history.canRedo;
  }

  setTool(tool) {
    const { model, tr, view } = this;
    const chosen = TOOLS.find((candidate) => candidate.id === tool);
    if (!chosen) return;
    model.tool = tool;
    for (const [id, toolButton] of this.toolButtons) {
      const active = id === tool;
      toolButton.classList.toggle("is-active", active);
      toolButton.setAttribute("aria-pressed", String(active));
    }
    view.shell.dataset.tool = tool;
    delete view.shell.dataset.bbox;
    view.renderRegionOverlay();
    this.toolName.replaceChildren(icon(tool), el("span", { text: tr(chosen.label) }));
    this.updateToolOptions();
    if (TEXT_TOOLS.has(tool)) this.showInspectorTab("text");
    this.setStatus(tr(chosen.label));
  }

  // The bar only offers what the tool in hand reads; the selection actions
  // stay while there is a selection, since they act on it whatever the tool.
  updateToolOptions() {
    const { model } = this;
    this.brushField.hidden = !BRUSH_TOOLS.has(model.tool);
    this.filledField.hidden = !SHAPE_TOOLS.has(model.tool);
    this.selectionGroup.hidden = model.tool !== "select" && model.selection === null;
  }

  showInspectorTab(key) {
    for (const tab of this.inspectorTabs) {
      tab.panel.hidden = tab.key !== key;
      tab.button.setAttribute("aria-selected", String(tab.key === key));
    }
  }

  renderAll() {
    this.view.applyZoom();
    this.view.renderCanvas();
    this.layers.render();
    this.timeline.render();
    this.text.renderRegions();
    this.updateHistoryButtons();
    this.updateSelectionButtons();
  }

  updateSelectionButtons() {
    const selected = this.model.selection !== null;
    this.copyButton.disabled = !selected;
    this.cutButton.disabled = !selected || this.currentLayer().locked;
    this.clearButton.disabled = !selected || this.currentLayer().locked;
    this.pasteButton.disabled = this.clipboard === null || this.currentLayer().locked;
    this.updateToolOptions();
  }

  editableLayer(clears = this.model.tool === "eraser") {
    const { model, tr } = this;
    const layer = this.currentLayer();
    if (!model.editable) {
      this.setStatus(tr("This image mode cannot be edited without loss."));
      return null;
    }
    if (layer.locked) {
      this.setStatus(tr("The active layer is locked."));
      return null;
    }
    if (clears && !model.hasAlpha) {
      this.setStatus(tr("Transparency tools are disabled for this image mode."));
      return null;
    }
    return layer;
  }

  undo() {
    this.timeline.stopPlayback();
    if (!this.history.undo(this.model)) return;
    this.showRestored();
  }

  redo() {
    this.timeline.stopPlayback();
    if (!this.history.redo(this.model)) return;
    this.showRestored();
  }

  showRestored() {
    this.text.syncInputs();
    this.markDirty();
    this.renderAll();
  }

  copySelection() {
    const { model } = this;
    if (model.selection === null) return;
    this.clipboard = copyRegion(this.currentLayer().canvas, model.selection);
    this.updateSelectionButtons();
    this.setStatus(this.tr("Selection copied."));
  }

  cutSelection() {
    const { model } = this;
    if (model.selection === null || this.editableLayer(true) === null) return;
    this.history.checkpoint(model);
    this.clipboard = copyRegion(this.currentLayer().canvas, model.selection);
    clearRegion(this.currentLayer().canvas, model.selection);
    this.markDirty();
    this.renderAll();
  }

  pasteSelection() {
    const { model, clipboard } = this;
    if (clipboard === null || this.editableLayer() === null) return;
    this.history.checkpoint(model);
    const x = model.selection?.x ?? 0;
    const y = model.selection?.y ?? 0;
    pasteRegion(this.currentLayer().canvas, clipboard, x, y);
    model.selection = {
      x,
      y,
      width: Math.min(clipboard.width, model.width - x),
      height: Math.min(clipboard.height, model.height - y),
    };
    this.markDirty();
    this.renderAll();
  }

  deleteSelection() {
    const { model } = this;
    if (model.selection === null || this.editableLayer(true) === null) return;
    this.history.checkpoint(model);
    clearRegion(this.currentLayer().canvas, model.selection);
    this.markDirty();
    this.renderAll();
  }

  async save() {
    const { model, tr } = this;
    if (this.saving) return false;
    this.timeline.stopPlayback();
    this.saving = true;
    this.saveButton.disabled = true;
    this.saveCloseButton.disabled = true;
    this.setStatus(tr("Saving..."));
    const saved = this.edits;
    try {
      const result = await this.bridge.save_image_editor_state(
        model.path,
        model.sourceFingerprint,
        serializeState(model),
      );
      model.outputFingerprint = result.output_fingerprint;
      if (this.edits === saved) this.markSaved();
      this.setStatus(tr("Image saved."));
      this.options.onSaved?.(result);
      return true;
    } catch (error) {
      this.setStatus(tr("Image save failed: {error}", { error: error.message }));
      return false;
    } finally {
      this.saving = false;
      this.saveButton.disabled = false;
      this.saveCloseButton.disabled = false;
    }
  }

  async saveAndClose() {
    if (await this.save()) this.options.onRequestClose?.(true);
  }

  onKeydown(event) {
    const { model } = this;
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName);
    if (event.ctrlKey && this.runShortcut(event, typing)) return;
    if (typing) return;
    if (event.key === "Delete") {
      event.preventDefault();
      if (model.tool === "bbox" && model.selectedRegion !== null) this.text.removeRegion(model.selectedRegion);
      else this.deleteSelection();
      return;
    }
    if (event.key === "Escape") {
      this.escape();
      return;
    }
    const nudge = NUDGE_KEYS[event.key];
    if (nudge && model.tool === "bbox" && model.selectedRegion !== null) {
      event.preventDefault();
      const step = event.shiftKey ? 10 : 1;
      this.text.nudgeBox(nudge[0] * step, nudge[1] * step);
      return;
    }
    if (event.key === " ") {
      event.preventDefault();
      this.timeline.togglePlayback();
      return;
    }
    this.pickToolOrZoom(event);
  }

  runShortcut(event, typing) {
    const shortcuts = {
      s: () => this.save(),
      z: () => (event.shiftKey ? this.redo() : this.undo()),
      y: () => this.redo(),
      c: typing ? null : () => this.copySelection(),
      x: typing ? null : () => this.cutSelection(),
      v: typing ? null : () => this.pasteSelection(),
    };
    const key = event.key.toLowerCase();
    const action = Object.hasOwn(shortcuts, key) ? shortcuts[key] : null;
    if (action === null) return false;
    event.preventDefault();
    action();
    return true;
  }

  escape() {
    const { model } = this;
    if (this.timeline.playbackFrame !== null) this.timeline.stopPlayback();
    else if (model.selection !== null) {
      model.selection = null;
      this.view.renderSelection();
      this.updateSelectionButtons();
    } else if (model.tool === "bbox" && model.selectedRegion !== null) {
      model.selectedRegion = null;
      this.text.renderRegions();
    } else {
      this.options.onRequestClose?.(false);
    }
  }

  pickToolOrZoom(event) {
    const tool = TOOLS.find((candidate) => candidate.shortcut.toLowerCase() === event.key.toLowerCase());
    if (tool) {
      event.preventDefault();
      this.setTool(tool.id);
    } else if (event.key === "+" || event.key === "=") {
      this.view.zoomIn();
    } else if (event.key === "-") {
      this.view.zoomOut();
    }
  }
}

class CanvasView {
  constructor(editor) {
    const { model, tr } = editor;
    this.editor = editor;
    this.displayCanvas = el("canvas", { class: "editor-display-canvas" });
    this.displayCanvas.width = model.width;
    this.displayCanvas.height = model.height;
    this.gridOverlay = el("div", { class: "editor-grid-overlay" });
    this.selectionOverlay = el("div", { class: "editor-selection-overlay", hidden: true });
    this.textOverlay = el("div", { class: "editor-text-overlay", hidden: true });
    this.regionOverlay = el("div", { class: "editor-region-overlay", hidden: true });
    this.shell = el("div", { class: "editor-canvas-shell" }, [
      this.displayCanvas,
      this.gridOverlay,
      this.selectionOverlay,
      this.textOverlay,
      this.regionOverlay,
    ]);
    this.viewport = el("div", { class: "editor-canvas-viewport" }, [this.shell]);
    this.coordinateStatus = el("span", { text: "x --, y --" });
    this.actionStatus = el("span", { class: "editor-action-status" });
    this.area = el("section", { class: "editor-canvas-area" }, [
      this.viewport,
      el("div", { class: "editor-statusbar" }, [
        this.coordinateStatus,
        el("span", { text: `${model.width} × ${model.height}` }),
        this.actionStatus,
      ]),
    ]);
    this.previewCanvas = el("canvas", { class: "editor-live-preview" });
    this.previewCanvas.width = model.width;
    this.previewCanvas.height = model.height;
    this.previewPanel = el("section", { class: "editor-inspector-section" }, [
      el("h3", { text: tr("Live 1:1 preview") }),
      el("div", { class: "editor-preview-shell" }, [this.previewCanvas]),
    ]);
    this.zoomLabel = el("span", { class: "editor-zoom-label" });
    this.viewport.addEventListener(
      "wheel",
      (event) => {
        if (!event.ctrlKey) return;
        event.preventDefault();
        if (event.deltaY < 0) this.zoomIn();
        else this.zoomOut();
      },
      { passive: false },
    );
  }

  get zoom() {
    return ZOOM_LEVELS[this.editor.model.zoomIndex];
  }

  get playbackFrame() {
    return this.editor.timeline.playbackFrame;
  }

  toPixel(event) {
    const { model } = this.editor;
    const rect = this.displayCanvas.getBoundingClientRect();
    return {
      x: Math.floor(((event.clientX - rect.left) / rect.width) * model.width),
      y: Math.floor(((event.clientY - rect.top) / rect.height) * model.height),
    };
  }

  contains(point) {
    const { model } = this.editor;
    return point.x >= 0 && point.y >= 0 && point.x < model.width && point.y < model.height;
  }

  showCoordinates(point) {
    this.coordinateStatus.textContent = `x ${point.x}, y ${point.y}`;
  }

  renderSelection() {
    const { selection } = this.editor.model;
    if (selection === null) {
      this.selectionOverlay.hidden = true;
      return;
    }
    this.selectionOverlay.hidden = false;
    placeRect(this.selectionOverlay, selection.x, selection.y, selection.width, selection.height, this.zoom);
  }

  renderTextOverlay() {
    const { textLayer } = this.editor.model;
    const overlay = this.textOverlay;
    if (textLayer.region === null || this.playbackFrame !== null) {
      overlay.hidden = true;
      return;
    }
    const zoom = this.zoom;
    const [x0, y0, x1, y1] = textLayer.region;
    overlay.hidden = false;
    overlay.textContent = textLayer.target;
    placeRect(overlay, x0, y0, x1 - x0, y1 - y0, zoom);
    overlay.style.fontFamily = this.editor.text.previewFamily();
    const { style } = textLayer;
    overlay.style.fontSize = `${style.font_size * zoom}px`;
    overlay.style.color = argbToCss(style.fill);
    overlay.style.backgroundColor = argbToCss(style.background);
    overlay.style.textAlign = style.alignment;
    const strokeWidth = style.stroke_width * zoom;
    overlay.style.webkitTextStroke = strokeWidth > 0 ? `${strokeWidth}px ${argbToCss(style.stroke)}` : "";
  }

  renderRegionOverlay() {
    const { model, regions } = this.editor;
    const overlay = this.regionOverlay;
    overlay.replaceChildren();
    overlay.hidden = regions.length === 0 || this.playbackFrame !== null;
    if (overlay.hidden) return;
    const zoom = this.zoom;
    regions.forEach((region, index) => overlay.append(regionNode(region, index, model, zoom)));
  }

  applyZoom() {
    const { model } = this.editor;
    const zoom = this.zoom;
    const width = model.width * zoom;
    const height = model.height * zoom;
    this.shell.style.width = `${width}px`;
    this.shell.style.height = `${height}px`;
    this.displayCanvas.style.width = `${width}px`;
    this.displayCanvas.style.height = `${height}px`;
    this.gridOverlay.style.backgroundSize = `${zoom}px ${zoom}px`;
    this.gridOverlay.hidden = !model.grid || zoom < 4;
    this.zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
    this.renderSelection();
    this.renderTextOverlay();
    this.renderRegionOverlay();
  }

  renderCanvas() {
    const { model } = this.editor;
    const index = this.playbackFrame ?? model.activeFrame;
    this.displayCanvas.getContext("2d").clearRect(0, 0, model.width, model.height);
    if (this.playbackFrame === null && model.onionSkin.enabled) this.drawOnionSkin();
    drawFrame(this.displayCanvas, model.frames[index]);
    const previewContext = this.previewCanvas.getContext("2d");
    previewContext.clearRect(0, 0, model.width, model.height);
    previewContext.drawImage(compositeFrame(model.frames[index], model.width, model.height), 0, 0);
    this.renderSelection();
    this.renderTextOverlay();
  }

  drawOnionSkin() {
    const { model } = this.editor;
    const { onionSkin } = model;
    for (let offset = onionSkin.previous_frames; offset >= 1; offset -= 1) {
      const frameIndex = model.activeFrame - offset;
      if (frameIndex >= 0) {
        drawFrame(this.displayCanvas, model.frames[frameIndex], onionSkin.opacity / offset);
      }
    }
    for (let offset = onionSkin.next_frames; offset >= 1; offset -= 1) {
      const frameIndex = model.activeFrame + offset;
      if (frameIndex < model.frames.length) {
        drawFrame(this.displayCanvas, model.frames[frameIndex], onionSkin.opacity / offset);
      }
    }
  }

  zoomIn() {
    const { model } = this.editor;
    model.zoomIndex = Math.min(ZOOM_LEVELS.length - 1, model.zoomIndex + 1);
    this.applyZoom();
  }

  zoomOut() {
    const { model } = this.editor;
    model.zoomIndex = Math.max(0, model.zoomIndex - 1);
    this.applyZoom();
  }

  fitZoom() {
    const { model } = this.editor;
    const style = window.getComputedStyle(this.viewport);
    const paddingX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const paddingY = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    const availableWidth = Math.max(100, this.viewport.clientWidth - paddingX);
    const availableHeight = Math.max(100, this.viewport.clientHeight - paddingY);
    const target = Math.min(availableWidth / model.width, availableHeight / model.height);
    let index = 0;
    for (let candidate = 0; candidate < ZOOM_LEVELS.length; candidate += 1) {
      if (ZOOM_LEVELS[candidate] <= target) index = candidate;
    }
    model.zoomIndex = index;
    this.applyZoom();
  }
}

class CanvasPointer {
  constructor(editor) {
    this.editor = editor;
    this.interaction = null;
    const canvas = editor.view.displayCanvas;
    canvas.addEventListener("pointerdown", (event) => this.begin(event));
    canvas.addEventListener("pointermove", (event) => this.move(event));
    canvas.addEventListener("pointerup", (event) => this.end(event));
    canvas.addEventListener("pointercancel", (event) => this.end(event));
    canvas.addEventListener("auxclick", (event) => {
      if (event.button === 1) event.preventDefault();
    });
    canvas.addEventListener("pointerleave", () => {
      if (this.interaction === null) editor.view.coordinateStatus.textContent = "x --, y --";
    });
    canvas.addEventListener("contextmenu", (event) => event.preventDefault());
  }

  // The grip is a fixed size on screen, so what it covers on the picture
  // shrinks as the view is zoomed in.
  grip() {
    return Math.max(1, Math.round(6 / ZOOM_LEVELS[this.editor.model.zoomIndex]));
  }

  capture(event) {
    this.editor.view.displayCanvas.setPointerCapture(event.pointerId);
  }

  begin(event) {
    const { model, view } = this.editor;
    this.editor.root.focus({ preventScroll: true });
    const point = view.toPixel(event);
    if (!view.contains(point)) return;
    view.showCoordinates(point);
    if (pointerToolForButton(model.tool, event.button) === "hand") this.beginPan(event);
    else if (model.tool === "select") this.beginSelection(event, point);
    else if (model.tool === "text") this.beginTextRegion(event, point);
    else if (model.tool === "bbox") this.beginBox(event, point);
    else if (model.tool === "eyedropper") this.pickColor(event, point);
    else this.beginPaint(event, point);
  }

  beginPan(event) {
    const { view } = this.editor;
    event.preventDefault();
    this.interaction = {
      kind: "hand",
      x: event.clientX,
      y: event.clientY,
      scrollLeft: view.viewport.scrollLeft,
      scrollTop: view.viewport.scrollTop,
    };
    view.shell.classList.add("is-panning");
    this.capture(event);
  }

  beginSelection(event, point) {
    const { model, view } = this.editor;
    this.interaction = { kind: "select", start: point };
    model.selection = normalizeRect(point, point, model.width, model.height);
    this.capture(event);
    view.renderSelection();
  }

  beginTextRegion(event, point) {
    const { editor } = this;
    const { model } = editor;
    editor.history.checkpoint(model);
    this.interaction = { kind: "text", start: point };
    model.textLayer.region = [point.x, point.y, point.x + 1, point.y + 1];
    this.capture(event);
    editor.markDirty();
    editor.view.renderTextOverlay();
  }

  beginBox(event, point) {
    const { editor } = this;
    const { model } = editor;
    const gesture = bboxGestureAt(editor.regions, point, this.grip(), model.selectedRegion);
    if (gesture.kind === "create") {
      editor.history.checkpoint(model);
      editor.regions.push({ box: [point.x, point.y, point.x + 1, point.y + 1], text: "" });
      model.selectedRegion = editor.regions.length - 1;
      this.interaction = { kind: "bbox", mode: "draw", start: point, checkpointed: true };
      editor.markDirty();
    } else {
      model.selectedRegion = gesture.index;
      // A press on a box that goes nowhere is how one is picked to rename or
      // delete, so nothing is spent -- or dirtied -- until it actually moves.
      this.interaction = {
        kind: "bbox",
        mode: gesture.kind,
        edges: gesture.edges,
        origin: point,
        box: [...editor.regions[gesture.index].box],
        checkpointed: false,
      };
    }
    this.capture(event);
    editor.text.renderRegions();
  }

  pickColor(event, point) {
    const { colors, view } = this.editor;
    const color = pixelAt(view.displayCanvas, point.x, point.y);
    if (event.button === 2) colors.setSecondary(color);
    else colors.setPrimary(color);
    colors.renderPalette();
  }

  beginPaint(event, point) {
    const { editor } = this;
    const { model } = editor;
    const layer = editor.editableLayer();
    if (layer === null) return;
    const color = editor.colors.forEvent(event);
    editor.history.checkpoint(model);
    if (model.tool === "fill" || model.tool === "replace") {
      const paint = model.tool === "fill" ? floodFill : replaceColor;
      if (paint(layer.canvas, point.x, point.y, color)) editor.markDirty();
      editor.renderAll();
      return;
    }
    if (["line", "rectangle", "ellipse"].includes(model.tool)) {
      this.interaction = {
        kind: "shape",
        start: point,
        color,
        baseImage: layer.canvas
          .getContext("2d", { willReadFrequently: true })
          .getImageData(0, 0, model.width, model.height),
      };
      this.drawShape(layer, point);
    } else {
      this.interaction = { kind: "stroke", last: point, color };
      drawBrush(layer.canvas, point.x, point.y, color, model.brushSize, model.tool === "eraser");
    }
    this.capture(event);
    editor.markDirty();
    editor.view.renderCanvas();
  }

  drawShape(layer, point) {
    const { model } = this.editor;
    const { start, color, baseImage } = this.interaction;
    if (baseImage) layer.canvas.getContext("2d").putImageData(baseImage, 0, 0);
    if (model.tool === "line") {
      drawLine(layer.canvas, start, point, color, model.brushSize);
    } else if (model.tool === "rectangle") {
      drawRectangle(layer.canvas, start, point, color, model.brushSize, model.filledShapes);
    } else if (model.tool === "ellipse") {
      drawEllipse(layer.canvas, start, point, color, model.brushSize, model.filledShapes);
    }
  }

  move(event) {
    const { model, view } = this.editor;
    const { interaction } = this;
    const point = view.toPixel(event);
    if (view.contains(point)) view.showCoordinates(point);
    if (interaction === null) {
      if (model.tool === "bbox" && view.contains(point)) this.showBoxCursor(point);
      return;
    }
    if (interaction.kind === "hand") {
      view.viewport.scrollLeft = interaction.scrollLeft - (event.clientX - interaction.x);
      view.viewport.scrollTop = interaction.scrollTop - (event.clientY - interaction.y);
      return;
    }
    if (interaction.kind === "bbox") {
      // A box pulled past the picture's edge stops on it rather than at the
      // last point the pointer was still over the picture.
      this.dragBox({
        x: Math.max(0, Math.min(model.width - 1, point.x)),
        y: Math.max(0, Math.min(model.height - 1, point.y)),
      });
      return;
    }
    if (!view.contains(point)) return;
    if (interaction.kind === "select") this.stretchSelection(point);
    else if (interaction.kind === "text") this.stretchTextRegion(point);
    else this.paintTo(point);
  }

  showBoxCursor(point) {
    const { editor } = this;
    editor.view.shell.dataset.bbox = bboxCursor(
      bboxGestureAt(editor.regions, point, this.grip(), editor.model.selectedRegion),
    );
  }

  stretchSelection(point) {
    const { editor } = this;
    const { model } = editor;
    model.selection = normalizeRect(this.interaction.start, point, model.width, model.height);
    editor.view.renderSelection();
    editor.updateSelectionButtons();
  }

  stretchTextRegion(point) {
    const { model, view } = this.editor;
    const region = normalizeRect(this.interaction.start, point, model.width, model.height);
    model.textLayer.region = [region.x, region.y, region.x + region.width, region.y + region.height];
    view.renderTextOverlay();
  }

  paintTo(point) {
    const { editor, interaction } = this;
    const { model } = editor;
    const layer = editor.currentLayer();
    if (interaction.kind === "shape") {
      this.drawShape(layer, point);
    } else if (interaction.kind === "stroke") {
      drawLine(layer.canvas, interaction.last, point, interaction.color, model.brushSize, model.tool === "eraser");
      interaction.last = point;
    }
    editor.view.renderCanvas();
  }

  dragBox(point) {
    const { editor, interaction } = this;
    const { model } = editor;
    const region = editor.regions[model.selectedRegion];
    if (region === undefined) return;
    if (!interaction.checkpointed) {
      if (point.x === interaction.origin.x && point.y === interaction.origin.y) return;
      editor.history.checkpoint(model);
      interaction.checkpointed = true;
      editor.markDirty();
    }
    if (interaction.mode === "draw") {
      const rect = normalizeRect(interaction.start, point, model.width, model.height);
      region.box = [rect.x, rect.y, rect.x + rect.width, rect.y + rect.height];
    } else {
      const dx = point.x - interaction.origin.x;
      const dy = point.y - interaction.origin.y;
      region.box =
        interaction.mode === "move"
          ? moveBox(interaction.box, dx, dy, model.width, model.height)
          : resizeBox(interaction.box, interaction.edges, dx, dy, model.width, model.height);
    }
    editor.view.renderRegionOverlay();
    editor.text.syncBoxFields();
  }

  end(event) {
    const { editor, interaction } = this;
    if (interaction === null) return;
    const { view } = editor;
    const bbox = interaction.kind === "bbox";
    const changed = bbox ? interaction.checkpointed : ["shape", "stroke", "text"].includes(interaction.kind);
    this.interaction = null;
    view.shell.classList.remove("is-panning");
    if (view.displayCanvas.hasPointerCapture(event.pointerId)) {
      view.displayCanvas.releasePointerCapture(event.pointerId);
    }
    if (changed) {
      editor.markDirty();
      editor.renderAll();
    } else if (bbox) {
      editor.text.renderRegions();
    } else {
      view.renderSelection();
      editor.updateSelectionButtons();
    }
  }
}

class ColorControls {
  constructor(editor) {
    const { tr } = editor;
    this.editor = editor;
    const alphaInput = () => el("input", { type: "number", min: "0", max: "255", value: "255", title: tr("Alpha") });
    this.primaryInput = el("input", {
      type: "color",
      value: "#000000",
      title: tr("Primary"),
      "aria-label": tr("Primary"),
    });
    this.primaryAlpha = alphaInput();
    this.secondaryInput = el("input", {
      type: "color",
      value: "#ffffff",
      title: tr("Secondary"),
      "aria-label": tr("Secondary"),
    });
    this.secondaryAlpha = alphaInput();
  }

  pair() {
    return el("div", { class: "editor-color-pair" }, [
      el("span", { class: "editor-color-chip editor-color-primary" }, [this.primaryInput]),
      el("span", { class: "editor-color-chip editor-color-secondary" }, [this.secondaryInput]),
    ]);
  }

  panel() {
    const { tr } = this.editor;
    this.grid = el("div", { class: "editor-palette" });
    this.addButton = iconAction("plus", tr("Add current color"), "editor-icon-button is-small", () =>
      this.addCurrentColor(),
    );
    this.notice = el("p", { class: "editor-panel-note" });
    return el("section", { class: "editor-inspector-section" }, [
      el("div", { class: "editor-panel-heading" }, [el("h3", { text: tr("Palette") }), this.addButton]),
      el("div", { class: "editor-alpha-fields" }, [
        field(tr("Primary alpha"), this.primaryAlpha),
        field(tr("Secondary alpha"), this.secondaryAlpha),
      ]),
      this.grid,
      this.notice,
    ]);
  }

  start() {
    const { model } = this.editor;
    const first = paletteColor(model.palette[0]);
    const second = paletteColor(model.palette[Math.min(4, model.palette.length - 1)]);
    this.setPrimary(rgbaFromHex(first.hex, first.alpha));
    this.setSecondary(rgbaFromHex(second.hex, second.alpha));
    this.primaryInput.disabled = model.paletteLocked;
    this.primaryAlpha.disabled = model.alphaLocked;
    this.secondaryInput.disabled = model.paletteLocked;
    this.secondaryAlpha.disabled = model.alphaLocked;
    this.renderPalette();
  }

  primary() {
    return rgbaFromHex(this.primaryInput.value, this.primaryAlpha.value);
  }

  secondary() {
    return rgbaFromHex(this.secondaryInput.value, this.secondaryAlpha.value);
  }

  forEvent(event) {
    return event.button === 2 ? this.secondary() : this.primary();
  }

  setPrimary(color) {
    this.primaryInput.value = hexFromRgba(color);
    this.primaryAlpha.value = String(color.a);
  }

  setSecondary(color) {
    this.secondaryInput.value = hexFromRgba(color);
    this.secondaryAlpha.value = String(color.a);
  }

  renderPalette() {
    const { model, tr } = this.editor;
    this.grid.replaceChildren(...model.palette.map((raw) => this.swatch(raw)));
    this.addButton.disabled = model.paletteLocked;
    this.notice.textContent = paletteNotice(model, tr);
  }

  swatch(raw) {
    const parsed = paletteColor(raw);
    const color = () => rgbaFromHex(parsed.hex, parsed.alpha);
    const swatch = button("", raw, "editor-palette-swatch");
    swatch.setAttribute("aria-label", raw);
    swatch.style.backgroundColor = `${parsed.hex}${parsed.alpha.toString(16).padStart(2, "0")}`;
    swatch.addEventListener("click", () => this.setPrimary(color()));
    swatch.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      this.setSecondary(color());
    });
    return swatch;
  }

  addCurrentColor() {
    const { palette } = this.editor.model;
    const color = this.primary();
    const value = `${hexFromRgba(color)}${color.a.toString(16).padStart(2, "0")}`;
    if (!palette.includes(value)) palette.push(value);
    this.renderPalette();
  }
}

class LayersPanel {
  constructor(editor) {
    const { tr } = editor;
    const small = "editor-icon-button is-small";
    this.editor = editor;
    this.list = el("div", { class: "editor-layers" });
    const addButton = iconAction("plus", tr("Add raster layer"), small, () => this.add());
    const duplicateButton = iconAction("duplicate", tr("Duplicate"), small, () => this.duplicate());
    this.removeButton = iconAction("trash", tr("Remove raster layer"), small, () => this.remove());
    this.upButton = iconAction("arrowUp", tr("Up"), small, () => this.move(1));
    this.downButton = iconAction("arrowDown", tr("Down"), small, () => this.move(-1));
    this.element = el("section", { class: "editor-inspector-section" }, [
      el("div", { class: "editor-panel-heading" }, [
        el("h3", { text: tr("Raster layers") }),
        el("div", { class: "editor-option-group" }, [
          addButton,
          duplicateButton,
          this.upButton,
          this.downButton,
          this.removeButton,
        ]),
      ]),
      this.list,
    ]);
  }

  render() {
    const { editor } = this;
    const { model } = editor;
    const scrollTop = this.list.scrollTop;
    const frame = editor.currentFrame();
    const rows = frame.layers.map((layer, index) => this.row(layer, index)).reverse();
    this.list.replaceChildren(...rows);
    this.list.scrollTop = scrollTop;
    this.removeButton.disabled = frame.layers.length <= 1;
    this.upButton.disabled = model.activeLayer >= frame.layers.length - 1;
    this.downButton.disabled = model.activeLayer <= 0;
  }

  row(layer, index) {
    const { editor } = this;
    const { model, tr } = editor;
    const visibilityButton = iconButton(layer.visible ? "eye" : "eyeOff", tr("Visible"), "editor-icon-button is-small");
    visibilityButton.setAttribute("aria-pressed", String(layer.visible));
    const lockButton = iconButton(layer.locked ? "lock" : "unlock", tr("Locked"), "editor-icon-button is-small");
    lockButton.setAttribute("aria-pressed", String(layer.locked));
    const row = el("div", { class: `editor-layer-row${index === model.activeLayer ? " is-active" : ""}` }, [
      visibilityButton,
      lockButton,
      layerThumbnail(layer),
      el("div", { class: "editor-layer-fields" }, [this.nameInput(layer), this.opacityInput(layer)]),
    ]);
    row.addEventListener("click", () => {
      model.activeLayer = index;
      this.render();
    });
    visibilityButton.addEventListener("click", (event) => {
      event.stopPropagation();
      editor.history.checkpoint(model);
      layer.visible = !layer.visible;
      editor.markDirty();
      editor.renderAll();
    });
    lockButton.addEventListener("click", (event) => {
      event.stopPropagation();
      editor.history.checkpoint(model);
      layer.locked = !layer.locked;
      editor.markDirty();
      this.render();
      editor.updateHistoryButtons();
    });
    return row;
  }

  nameInput(layer) {
    const { editor } = this;
    const input = el("input", { type: "text", value: layer.name });
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("change", () => {
      const name = input.value.trim();
      if (!name || name === layer.name) {
        input.value = layer.name;
        return;
      }
      editor.history.checkpoint(editor.model);
      layer.name = name;
      editor.markDirty();
      this.render();
      editor.updateHistoryButtons();
    });
    return input;
  }

  opacityInput(layer) {
    const { editor } = this;
    const input = el("input", {
      type: "range",
      min: "0",
      max: "1",
      step: "0.05",
      value: String(layer.opacity),
      title: editor.tr("Opacity"),
    });
    input.addEventListener("pointerdown", () => {
      if (input.dataset.history !== "true") {
        editor.history.checkpoint(editor.model);
        input.dataset.history = "true";
      }
    });
    input.addEventListener("input", () => {
      layer.opacity = Number(input.value);
      editor.markDirty();
      editor.view.renderCanvas();
    });
    input.addEventListener("change", () => {
      delete input.dataset.history;
      editor.renderAll();
      editor.updateHistoryButtons();
    });
    return input;
  }

  add() {
    const { editor } = this;
    const { model, tr } = editor;
    editor.history.checkpoint(model);
    const frame = editor.currentFrame();
    const ids = new Set(frame.layers.map((layer) => layer.id));
    frame.layers.push({
      id: nextIdentifier("layer", ids),
      name: `${tr("Layer")} ${frame.layers.length + 1}`,
      visible: true,
      opacity: 1,
      locked: false,
      canvas: makeCanvas(model.width, model.height),
    });
    model.activeLayer = frame.layers.length - 1;
    editor.markDirty();
    editor.renderAll();
  }

  duplicate() {
    const { editor } = this;
    const { model, tr } = editor;
    editor.history.checkpoint(model);
    const frame = editor.currentFrame();
    const layer = editor.currentLayer();
    const ids = new Set(frame.layers.map((candidate) => candidate.id));
    frame.layers.splice(model.activeLayer + 1, 0, {
      ...layer,
      id: nextIdentifier("layer", ids),
      name: `${layer.name} ${tr("Copy")}`,
      canvas: cloneCanvas(layer.canvas),
    });
    model.activeLayer += 1;
    editor.markDirty();
    editor.renderAll();
  }

  remove() {
    const { editor } = this;
    const { model } = editor;
    const frame = editor.currentFrame();
    if (frame.layers.length <= 1) return;
    editor.history.checkpoint(model);
    frame.layers.splice(model.activeLayer, 1);
    model.activeLayer = Math.min(model.activeLayer, frame.layers.length - 1);
    editor.markDirty();
    editor.renderAll();
  }

  move(direction) {
    const { editor } = this;
    const { model } = editor;
    const frame = editor.currentFrame();
    const target = model.activeLayer + direction;
    if (target < 0 || target >= frame.layers.length) return;
    editor.history.checkpoint(model);
    [frame.layers[model.activeLayer], frame.layers[target]] = [frame.layers[target], frame.layers[model.activeLayer]];
    model.activeLayer = target;
    editor.markDirty();
    editor.renderAll();
  }
}

class TextPanel {
  constructor(editor) {
    this.editor = editor;
    // The face a layer naming none is typeset in: the project's own.
    this.typesetFont = { font: null, default: null };
    // The faces that draw every picture's translation; null while being read.
    this.typesetFonts = null;
    this.previewFamilies = new Map();
    this.count = el("span", { class: "editor-tab-count" });
    const typesetting = this.typesettingSection();
    this.sections = [this.regionsSection(), typesetting];
  }

  typesettingSection() {
    const { tr } = this.editor;
    this.sourceText = el("p", { class: "editor-source-text", lang: "ja" });
    this.targetInput = el("textarea", { rows: "3" });
    this.fontFamilySelect = el("select", {});
    this.fontSizeInput = el("input", { type: "number", min: "1" });
    this.fillInput = el("input", { type: "text" });
    this.strokeInput = el("input", { type: "text" });
    this.strokeWidthInput = el("input", { type: "number", min: "0" });
    this.backgroundInput = el("input", { type: "text" });
    this.alignmentSelect = el("select", {}, [
      el("option", { value: "left", text: tr("Left") }),
      el("option", { value: "center", text: tr("Center") }),
      el("option", { value: "right", text: tr("Right") }),
    ]);
    const section = el("section", { class: "editor-inspector-section editor-text-panel" }, [
      el("h3", { text: tr("Text layer") }),
      el("div", { class: "editor-field" }, [el("span", { text: tr("OCR source") }), this.sourceText]),
      field(tr("Translation"), this.targetInput),
      field(tr("Font"), this.fontFamilySelect),
      el("div", { class: "editor-field-pair" }, [
        field(tr("Size"), this.fontSizeInput),
        field(tr("Alignment"), this.alignmentSelect),
        field(tr("Fill"), this.fillInput),
        field(tr("Background"), this.backgroundInput),
        field(tr("Outline"), this.strokeInput),
        field(tr("Outline width"), this.strokeWidthInput),
      ]),
    ]);
    for (const input of [
      this.targetInput,
      this.fontFamilySelect,
      this.fontSizeInput,
      this.fillInput,
      this.strokeInput,
      this.strokeWidthInput,
      this.backgroundInput,
      this.alignmentSelect,
    ]) {
      this.checkpointInput(input, () => this.updateTextLayer());
    }
    return section;
  }

  regionsSection() {
    const { tr } = this.editor;
    this.list = el("div", { class: "editor-regions" });
    this.notice = el("p", { class: "editor-panel-note" });
    this.boxInputs = [0, 0, 1, 1].map((min) => el("input", { type: "number", min: String(min), step: "1" }));
    const [boxX, boxY, boxWidth, boxHeight] = this.boxInputs;
    this.boxFields = el("div", { class: "editor-box-fields" }, [
      field("X", boxX),
      field("Y", boxY),
      field(tr("Width"), boxWidth),
      field(tr("Height"), boxHeight),
    ]);
    for (const input of this.boxInputs) {
      this.checkpointInput(input, () => this.applyBoxFields());
      input.addEventListener("change", () => this.syncBoxFields());
    }
    return el("section", { class: "editor-inspector-section" }, [
      el("div", { class: "editor-panel-heading" }, [el("h3", { text: tr("OCR boxes") })]),
      this.list,
      this.boxFields,
      this.notice,
    ]);
  }

  checkpointInput(input, apply) {
    const { editor } = this;
    input.addEventListener("focus", () => {
      if (input.dataset.history !== "true") {
        editor.history.checkpoint(editor.model);
        input.dataset.history = "true";
      }
    });
    input.addEventListener("blur", () => {
      delete input.dataset.history;
      editor.updateHistoryButtons();
    });
    input.addEventListener("input", apply);
  }

  showFontChoices() {
    const { model, t, tr } = this.editor;
    const value = model.textLayer.style.font_family;
    const project = this.typesetFont.font ?? this.typesetFont.default;
    const nodes = fontOptionElements([
      {
        value: "",
        label: project?.name ? tr("Project font ({name})", { name: project.name }) : tr("Project font"),
      },
      ...listedFontOptions(value ? { path: value, name: null } : null, this.typesetFonts, t),
    ]);
    if (this.typesetFonts === null) {
      nodes.push(el("option", { disabled: true, text: tr("Loading fonts…") }));
    }
    this.fontFamilySelect.replaceChildren(...nodes);
    this.fontFamilySelect.value = value;
  }

  async loadFontChoices() {
    const { bridge, view } = this.editor;
    this.typesetFont = await bridge.typeset_font();
    this.showFontChoices();
    view.renderTextOverlay();
    this.typesetFonts = (await bridge.typeset_fonts()).fonts;
    this.showFontChoices();
  }

  // The family the overlay is lettered in: the face the layer will be typeset
  // in once that has loaded, and the name the layer gives until then.
  previewFamily() {
    const { editor } = this;
    const { style } = editor.model.textLayer;
    const ref = style.font_family || (this.typesetFont.font ?? this.typesetFont.default)?.path;
    if (!ref) return "";
    if (!this.previewFamilies.has(ref)) {
      this.previewFamilies.set(ref, null);
      loadFace(editor.bridge, ref).then((family) => {
        this.previewFamilies.set(ref, family);
        if (family && !editor.destroyed) editor.view.renderTextOverlay();
      });
    }
    return this.previewFamilies.get(ref) ?? style.font_family;
  }

  syncInputs() {
    const { textLayer } = this.editor.model;
    this.sourceText.textContent = textLayer.source || "";
    this.targetInput.value = textLayer.target || "";
    this.showFontChoices();
    const { style } = textLayer;
    this.fontSizeInput.value = String(style.font_size);
    this.fillInput.value = style.fill;
    this.strokeInput.value = style.stroke;
    this.strokeWidthInput.value = String(style.stroke_width);
    this.backgroundInput.value = style.background;
    this.alignmentSelect.value = style.alignment;
  }

  updateTextLayer() {
    const { editor } = this;
    const { textLayer } = editor.model;
    textLayer.target = this.targetInput.value;
    textLayer.style = {
      font_family: this.fontFamilySelect.value,
      font_size: Math.max(1, Number(this.fontSizeInput.value) || 1),
      fill: this.fillInput.value,
      stroke: this.strokeInput.value,
      stroke_width: Math.max(0, Number(this.strokeWidthInput.value) || 0),
      background: this.backgroundInput.value,
      alignment: this.alignmentSelect.value,
    };
    editor.markDirty();
    editor.view.renderTextOverlay();
    this.renderNotice();
  }

  renderRegions() {
    const { editor } = this;
    const { model } = editor;
    if (model.selectedRegion !== null && model.selectedRegion >= editor.regions.length) {
      model.selectedRegion = null;
    }
    editor.view.renderRegionOverlay();
    this.list.replaceChildren(...editor.regions.map((region, index) => this.regionRow(region, index)));
    this.renderNotice();
    this.syncBoxFields();
  }

  regionRow(region, index) {
    const { editor } = this;
    const { model, tr } = editor;
    const textInput = el("input", { type: "text", value: region.text, title: tr("OCR source"), lang: "ja" });
    const removeButton = iconAction("trash", tr("Remove OCR box"), "editor-icon-button is-small", (event) => {
      event.stopPropagation();
      this.removeRegion(index);
    });
    const row = el("div", { class: `editor-region-row${index === model.selectedRegion ? " is-active" : ""}` }, [
      el("span", { class: "editor-region-number", text: String(index + 1) }),
      textInput,
      removeButton,
    ]);
    // Picking a box from the list takes up the tool that moves it, so its
    // grips are on the picture ready to be pulled.
    row.addEventListener("click", () => {
      model.selectedRegion = index;
      if (model.tool !== "bbox") editor.setTool("bbox");
      this.renderRegions();
    });
    textInput.addEventListener("click", (event) => event.stopPropagation());
    this.checkpointInput(textInput, () => {
      region.text = textInput.value;
      editor.markDirty();
      editor.view.renderRegionOverlay();
    });
    return row;
  }

  renderNotice() {
    const { editor } = this;
    const { tr } = editor;
    const boxes = editor.regions.length;
    this.count.textContent = boxes > 0 ? String(boxes) : "";
    if (boxes === 0) {
      this.notice.textContent = tr("Draw a box with the OCR box tool (K) to mark text on this picture.");
      return;
    }
    const { target } = editor.model.textLayer;
    const lines = target ? target.split("\n").length : 0;
    this.notice.textContent =
      lines === boxes
        ? tr("{count} boxes, one translated line each.", { count: boxes })
        : tr("{count} boxes against {lines} translated lines. Typesetting needs one line per box.", {
            count: boxes,
            lines,
          });
  }

  syncBoxFields() {
    const { editor } = this;
    const { model } = editor;
    const region = editor.regions[model.selectedRegion];
    this.boxFields.hidden = region === undefined;
    if (region === undefined) return;
    const [x0, y0, x1, y1] = region.box;
    const values = [x0, y0, x1 - x0, y1 - y0];
    this.boxInputs.forEach((input, index) => {
      input.value = String(values[index]);
    });
    const [boxX, boxY, boxWidth, boxHeight] = this.boxInputs;
    boxX.max = String(model.width - 1);
    boxY.max = String(model.height - 1);
    boxWidth.max = String(model.width - x0);
    boxHeight.max = String(model.height - y0);
  }

  applyBoxFields() {
    const { editor } = this;
    const { model } = editor;
    const region = editor.regions[model.selectedRegion];
    if (region === undefined) return;
    const [boxX, boxY, boxWidth, boxHeight] = this.boxInputs;
    const [x0, y0, x1, y1] = region.box;
    const x = Math.max(0, Math.min(model.width - 1, readInteger(boxX, x0)));
    const y = Math.max(0, Math.min(model.height - 1, readInteger(boxY, y0)));
    const width = Math.max(1, Math.min(model.width - x, readInteger(boxWidth, x1 - x0)));
    const height = Math.max(1, Math.min(model.height - y, readInteger(boxHeight, y1 - y0)));
    region.box = [x, y, x + width, y + height];
    editor.markDirty();
    editor.view.renderRegionOverlay();
  }

  removeRegion(index) {
    const { editor } = this;
    if (index < 0 || index >= editor.regions.length) return;
    editor.history.checkpoint(editor.model);
    editor.regions.splice(index, 1);
    editor.model.selectedRegion = null;
    editor.markDirty();
    this.renderRegions();
    editor.updateHistoryButtons();
  }

  nudgeBox(dx, dy) {
    const { editor } = this;
    const { model } = editor;
    const region = editor.regions[model.selectedRegion];
    if (region === undefined) return;
    editor.history.checkpoint(model);
    region.box = moveBox(region.box, dx, dy, model.width, model.height);
    editor.markDirty();
    this.renderRegions();
    editor.updateHistoryButtons();
  }
}

class Timeline {
  constructor(editor) {
    const { tr } = editor;
    this.editor = editor;
    this.playbackTimer = null;
    this.playbackFrame = null;
    this.playButton = iconAction(
      "play",
      tr("Play"),
      "editor-icon-button editor-play-button",
      () => this.togglePlayback(),
      "Space",
    );
    const addFrameButton = iconAction("plus", tr("Add frame"), "editor-icon-button", () => this.addFrame());
    const duplicateFrameButton = iconAction("duplicate", tr("Duplicate frame"), "editor-icon-button", () =>
      this.duplicateFrame(),
    );
    this.removeFrameButton = iconAction("trash", tr("Remove frame"), "editor-icon-button", () => this.removeFrame());
    this.strip = el("div", { class: "editor-frame-strip", id: uniqueId("editor-frames") });
    this.count = el("span", { class: "editor-frame-count" });
    this.toggle = el(
      "button",
      { class: "editor-timeline-toggle", type: "button", "aria-controls": this.strip.id },
      [icon("chevron"), el("strong", { text: tr("Animation timeline") }), this.count],
    );
    this.toggle.addEventListener("click", () => this.show(this.toggle.getAttribute("aria-expanded") !== "true"));
    this.controls = el("div", { class: "editor-timeline-controls" }, [
      this.playButton,
      addFrameButton,
      duplicateFrameButton,
      this.removeFrameButton,
      el("span", { class: "editor-separator" }),
      ...this.onionControls(),
    ]);
    this.element = el("section", { class: "editor-timeline" }, [
      el("div", { class: "editor-timeline-heading" }, [this.toggle, this.controls]),
      this.strip,
    ]);
  }

  onionControls() {
    const { model, tr } = this.editor;
    const onion = model.onionSkin;
    const enabledInput = el("input", { type: "checkbox" });
    enabledInput.checked = onion.enabled;
    const frameCountInput = (value) => el("input", { type: "number", min: "0", max: "8", value: String(value) });
    const previousInput = frameCountInput(onion.previous_frames);
    const nextInput = frameCountInput(onion.next_frames);
    const opacityInput = el("input", {
      type: "range",
      min: "0.05",
      max: "0.9",
      step: "0.05",
      value: String(onion.opacity),
    });
    this.onionSetting(enabledInput, (skin) => {
      skin.enabled = enabledInput.checked;
    });
    this.onionSetting(previousInput, (skin) => {
      skin.previous_frames = Math.max(0, Number(previousInput.value) || 0);
    });
    this.onionSetting(nextInput, (skin) => {
      skin.next_frames = Math.max(0, Number(nextInput.value) || 0);
    });
    this.onionSetting(opacityInput, (skin) => {
      skin.opacity = Number(opacityInput.value);
    });
    return [
      el("label", { class: "editor-check" }, [enabledInput, tr("Onion skin")]),
      field(tr("Previous"), previousInput, true),
      field(tr("Next"), nextInput, true),
      field(tr("Opacity"), opacityInput, true),
    ];
  }

  onionSetting(input, apply) {
    const { editor } = this;
    input.addEventListener("change", () => {
      editor.history.checkpoint(editor.model);
      apply(editor.model.onionSkin);
      editor.markDirty();
      editor.view.renderCanvas();
      editor.updateHistoryButtons();
    });
  }

  show(open) {
    this.toggle.setAttribute("aria-expanded", String(open));
    this.controls.hidden = !open;
    this.strip.hidden = !open;
  }

  showPlaying(playing) {
    const { tr } = this.editor;
    const label = playing ? tr("Stop") : tr("Play");
    setIcon(this.playButton, playing ? "stop" : "play");
    this.playButton.setAttribute("aria-label", label);
    this.playButton.title = `${label} (Space)`;
    this.playButton.classList.toggle("is-active", playing);
  }

  render() {
    const { model, tr } = this.editor;
    const scrollLeft = this.strip.scrollLeft;
    this.strip.replaceChildren(...model.frames.map((frame, index) => this.card(frame, index)));
    this.strip.scrollLeft = scrollLeft;
    this.count.textContent = tr("Frames: {count}", { count: model.frames.length });
    this.removeFrameButton.disabled = model.frames.length <= 1;
  }

  card(frame, index) {
    const { editor } = this;
    const { model, tr } = editor;
    const active = index === model.activeFrame ? " is-active" : "";
    const playing = index === this.playbackFrame ? " is-playing" : "";
    const card = el(
      "div",
      {
        class: `editor-frame-card${active}${playing}`,
        role: "button",
        tabindex: "0",
        "aria-label": tr("Frame {number}", { number: index + 1 }),
      },
      [
        frameThumbnail(frame, model),
        el("span", { class: "editor-frame-number", text: String(index + 1) }),
        ...this.frameFields(frame, index),
      ],
    );
    card.dataset.frameIndex = String(index);
    card.addEventListener("click", () => {
      model.activeFrame = index;
      model.activeLayer = Math.min(model.activeLayer, frame.layers.length - 1);
      model.selection = null;
      editor.renderAll();
    });
    card.addEventListener("keydown", (event) => {
      if (event.target !== card) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      card.click();
    });
    return card;
  }

  frameFields(frame, index) {
    const { editor } = this;
    const { model, tr } = editor;
    const durationInput = el("input", {
      type: "number",
      min: "1",
      max: "60000",
      value: String(frame.duration_ms),
      title: tr("Duration"),
    });
    const outputButton = iconButton("flag", tr("Use for output"), "editor-icon-button is-small editor-frame-output");
    outputButton.setAttribute("aria-pressed", String(index === model.outputFrame));
    durationInput.addEventListener("click", (event) => event.stopPropagation());
    durationInput.addEventListener("change", () => {
      editor.history.checkpoint(model);
      frame.duration_ms = Math.max(1, Number(durationInput.value) || 100);
      editor.markDirty();
      this.render();
      editor.updateHistoryButtons();
    });
    outputButton.addEventListener("click", (event) => {
      event.stopPropagation();
      if (model.outputFrame === index) return;
      editor.history.checkpoint(model);
      model.outputFrame = index;
      editor.markDirty();
      this.render();
      editor.updateHistoryButtons();
    });
    return [durationInput, outputButton];
  }

  // A new frame copies the current one's layers -- names, visibility, opacity --
  // and takes its pixels from canvasFor.
  spliceFrame(canvasFor) {
    const { editor } = this;
    const { model } = editor;
    this.stopPlayback();
    editor.history.checkpoint(model);
    const source = editor.currentFrame();
    const ids = new Set(model.frames.map((frame) => frame.id));
    const frame = {
      id: nextIdentifier("frame", ids),
      duration_ms: source.duration_ms,
      layers: source.layers.map((layer) => ({ ...layer, canvas: canvasFor(layer) })),
    };
    insertFrame(model, frame);
    return frame;
  }

  addFrame() {
    const { editor } = this;
    const { model } = editor;
    const frame = this.spliceFrame(() => makeCanvas(model.width, model.height));
    model.activeLayer = Math.min(model.activeLayer, frame.layers.length - 1);
    editor.markDirty();
    editor.renderAll();
  }

  duplicateFrame() {
    const { editor } = this;
    this.spliceFrame((layer) => cloneCanvas(layer.canvas));
    editor.markDirty();
    editor.renderAll();
  }

  removeFrame() {
    const { editor } = this;
    const { model } = editor;
    if (model.frames.length <= 1) return;
    this.stopPlayback();
    editor.history.checkpoint(model);
    const removed = model.activeFrame;
    model.frames.splice(removed, 1);
    model.activeFrame = Math.min(removed, model.frames.length - 1);
    if (model.outputFrame === removed) model.outputFrame = model.activeFrame;
    else if (model.outputFrame > removed) model.outputFrame -= 1;
    model.activeLayer = Math.min(model.activeLayer, editor.currentFrame().layers.length - 1);
    editor.markDirty();
    editor.renderAll();
  }

  schedulePlayback() {
    const { editor } = this;
    if (this.playbackFrame === null || editor.destroyed) return;
    const duration = editor.model.frames[this.playbackFrame].duration_ms;
    this.playbackTimer = window.setTimeout(() => {
      this.playbackFrame = (this.playbackFrame + 1) % editor.model.frames.length;
      editor.view.renderCanvas();
      this.renderPlaybackMarker();
      this.schedulePlayback();
    }, duration);
  }

  renderPlaybackMarker() {
    for (const card of this.strip.querySelectorAll(".editor-frame-card")) {
      card.classList.toggle("is-playing", Number(card.dataset.frameIndex) === this.playbackFrame);
    }
  }

  startPlayback() {
    const { editor } = this;
    if (editor.model.frames.length < 2) {
      editor.setStatus(editor.tr("Add another frame to start playback."));
      return;
    }
    this.playbackFrame = editor.model.activeFrame;
    this.showPlaying(true);
    this.schedulePlayback();
    editor.renderAll();
  }

  stopPlayback() {
    const wasPlaying = this.playbackFrame !== null || this.playbackTimer !== null;
    this.cancelTimer();
    this.playbackFrame = null;
    this.showPlaying(false);
    if (wasPlaying && !this.editor.destroyed) this.editor.renderAll();
  }

  cancelTimer() {
    if (this.playbackTimer !== null) window.clearTimeout(this.playbackTimer);
    this.playbackTimer = null;
  }

  togglePlayback() {
    if (this.playbackFrame === null) this.startPlayback();
    else this.stopPlayback();
  }
}
