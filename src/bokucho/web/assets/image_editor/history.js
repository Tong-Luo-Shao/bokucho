// Memory-capped snapshot history for raster, frame, layer, and text changes.

import { makeCanvas } from "./canvas.js";

function cloneValue(value) {
  return JSON.parse(JSON.stringify(value));
}

function capture(model) {
  let bytes = 0;
  const frames = model.frames.map((frame) => ({
    id: frame.id,
    duration_ms: frame.duration_ms,
    layers: frame.layers.map((layer) => {
      const image = layer.canvas
        .getContext("2d", { willReadFrequently: true })
        .getImageData(0, 0, model.width, model.height);
      bytes += image.data.byteLength;
      return {
        id: layer.id,
        name: layer.name,
        visible: layer.visible,
        opacity: layer.opacity,
        locked: layer.locked,
        image,
      };
    }),
  }));
  return {
    bytes,
    frames,
    activeFrame: model.activeFrame,
    outputFrame: model.outputFrame,
    activeLayer: model.activeLayer,
    onionSkin: cloneValue(model.onionSkin),
    textLayer: cloneValue(model.textLayer),
    selection: model.selection ? { ...model.selection } : null,
  };
}

function restore(model, snapshot) {
  model.frames = snapshot.frames.map((frame) => ({
    id: frame.id,
    duration_ms: frame.duration_ms,
    layers: frame.layers.map((layer) => {
      const canvas = makeCanvas(model.width, model.height);
      canvas.getContext("2d").putImageData(layer.image, 0, 0);
      return {
        id: layer.id,
        name: layer.name,
        visible: layer.visible,
        opacity: layer.opacity,
        locked: layer.locked,
        canvas,
      };
    }),
  }));
  model.activeFrame = snapshot.activeFrame;
  model.outputFrame = snapshot.outputFrame;
  model.activeLayer = snapshot.activeLayer;
  model.onionSkin = cloneValue(snapshot.onionSkin);
  model.textLayer = cloneValue(snapshot.textLayer);
  model.selection = snapshot.selection ? { ...snapshot.selection } : null;
}

export class EditorHistory {
  constructor(maxEntries = 24, maxBytes = 96 * 1024 * 1024) {
    this.maxEntries = maxEntries;
    this.maxBytes = maxBytes;
    this.past = [];
    this.future = [];
  }

  checkpoint(model) {
    this.past.push(capture(model));
    this.future = [];
    this.trim();
  }

  undo(model) {
    const previous = this.past.pop();
    if (!previous) return false;
    this.future.push(capture(model));
    restore(model, previous);
    return true;
  }

  redo(model) {
    const next = this.future.pop();
    if (!next) return false;
    this.past.push(capture(model));
    restore(model, next);
    return true;
  }

  get canUndo() {
    return this.past.length > 0;
  }

  get canRedo() {
    return this.future.length > 0;
  }

  trim() {
    while (this.past.length > this.maxEntries) this.past.shift();
    let total = this.past.reduce((sum, snapshot) => sum + snapshot.bytes, 0);
    while (this.past.length > 1 && total > this.maxBytes) {
      total -= this.past.shift().bytes;
    }
  }
}
