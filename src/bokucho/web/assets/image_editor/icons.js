// Line icons for the image editor, drawn on one 16px grid with one stroke so
// every tool reads as part of the same set.

const SVG_NS = "http://www.w3.org/2000/svg";

const ICONS = {
  pencil: { stroke: ["M10.6 2.6a1.5 1.5 0 0 1 2.1 0l.7.7a1.5 1.5 0 0 1 0 2.1L5.5 13.3l-3 .7.7-3z", "M9.3 3.9l2.8 2.8"] },
  eraser: {
    stroke: [
      "M6.4 13.5h7.1",
      "M2.9 9.6l6-6a1.4 1.4 0 0 1 2 0l2.5 2.5a1.4 1.4 0 0 1 0 2l-5 5H6.6l-3.7-1.5a1.4 1.4 0 0 1 0-2z",
      "M6.2 6.3l4.5 4.5",
    ],
  },
  fill: {
    stroke: ["M7 2.5l5.2 5.2-4.6 4.6a1.4 1.4 0 0 1-2 0L2.3 9a1.4 1.4 0 0 1 0-2z", "M2.4 8.1h9.6"],
    fill: ["M13.4 10.4s1.3 1.5 1.3 2.4a1.3 1.3 0 0 1-2.6 0c0-.9 1.3-2.4 1.3-2.4z"],
  },
  eyedropper: {
    stroke: [
      "M9.6 3.1a2 2 0 0 1 2.8 0l.5.5a2 2 0 0 1 0 2.8l-1.4 1.4-3.3-3.3z",
      "M8.9 5.8l-5.6 5.6-.8 2.1 2.1-.8 5.6-5.6",
    ],
  },
  replace: {
    stroke: ["M2.5 2.5h5v5h-5z", "M10 3.5h1.5a2 2 0 0 1 2 2V7", "M12 5.6l1.5 1.5L15 5.6", "M6 12.5H4.5a2 2 0 0 1-2-2V9", "M1 10.4l1.5-1.5L4 10.4"],
    fill: ["M8.5 8.5h5v5h-5z"],
  },
  line: { stroke: ["M3.5 12.5l9-9"], fill: ["M1.8 11.8h2.4v2.4H1.8z", "M11.8 1.8h2.4v2.4h-2.4z"] },
  rectangle: { stroke: ["M3 3.5h10a.5.5 0 0 1 .5.5v8a.5.5 0 0 1-.5.5H3a.5.5 0 0 1-.5-.5V4a.5.5 0 0 1 .5-.5z"] },
  ellipse: { stroke: ["M14 8c0 2.5-2.7 4.5-6 4.5S2 10.5 2 8s2.7-4.5 6-4.5 6 2 6 4.5z"] },
  select: {
    stroke: [
      "M2.5 5V2.5H5",
      "M7 2.5h2",
      "M11 2.5h2.5V5",
      "M13.5 7v2",
      "M13.5 11v2.5H11",
      "M9 13.5H7",
      "M5 13.5H2.5V11",
      "M2.5 9V7",
    ],
  },
  text: { stroke: ["M3 4V2.5h10V4", "M8 2.5v11", "M6 13.5h4"] },
  bbox: {
    stroke: ["M2.5 5.5v-3h3", "M10.5 2.5h3v3", "M13.5 10.5v3h-3", "M5.5 13.5h-3v-3", "M5.5 6h5", "M8 6v4.5"],
  },
  hand: {
    stroke: [
      "M5.5 8.5V3.8a1 1 0 0 1 2 0v3.7",
      "M7.5 7V2.8a1 1 0 0 1 2 0V7",
      "M9.5 7.2V4a1 1 0 0 1 2 0v4",
      "M11.5 6.5a1 1 0 0 1 2 0v3.3a4.7 4.7 0 0 1-4.7 4.7h-.5a4.6 4.6 0 0 1-3.6-1.7L2.6 10.2a1 1 0 0 1 1.5-1.3l1.4 1.3V8.5",
    ],
  },
  undo: { stroke: ["M5.5 3.5l-3 3 3 3", "M2.8 6.5h6.7a3.5 3.5 0 0 1 0 7H7"] },
  redo: { stroke: ["M10.5 3.5l3 3-3 3", "M13.2 6.5H6.5a3.5 3.5 0 0 0 0 7H9"] },
  copy: {
    stroke: [
      "M6 5.5h6.5a.5.5 0 0 1 .5.5v7a.5.5 0 0 1-.5.5H6a.5.5 0 0 1-.5-.5V6a.5.5 0 0 1 .5-.5z",
      "M10.5 5.5V3a.5.5 0 0 0-.5-.5H3.5A.5.5 0 0 0 3 3v7a.5.5 0 0 0 .5.5h2",
    ],
  },
  cut: {
    stroke: [
      "M6.5 11.5a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
      "M13.5 11.5a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
      "M5.6 9.8 12 2.5",
      "M10.4 9.8 4 2.5",
    ],
  },
  paste: {
    stroke: [
      "M5.5 3H4.3a.8.8 0 0 0-.8.8v9.4a.8.8 0 0 0 .8.8h7.4a.8.8 0 0 0 .8-.8V3.8a.8.8 0 0 0-.8-.8h-1.2",
      "M6.2 2h3.6a.7.7 0 0 1 .7.7v1.1a.7.7 0 0 1-.7.7H6.2a.7.7 0 0 1-.7-.7V2.7a.7.7 0 0 1 .7-.7z",
    ],
  },
  trash: {
    stroke: ["M2.5 4.5h11", "M6 4.5V3a.5.5 0 0 1 .5-.5h3a.5.5 0 0 1 .5.5v1.5", "M4 4.5l.7 8.6a.5.5 0 0 0 .5.4h5.6a.5.5 0 0 0 .5-.4L12 4.5", "M6.8 7v4", "M9.2 7v4"],
  },
  zoomIn: { stroke: ["M11.5 7a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0z", "M10.3 10.3 14 14", "M5 7h4", "M7 5v4"] },
  zoomOut: { stroke: ["M11.5 7a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0z", "M10.3 10.3 14 14", "M5 7h4"] },
  fit: { stroke: ["M2.5 6V2.5H6", "M10 2.5h3.5V6", "M13.5 10v3.5H10", "M6 13.5H2.5V10"] },
  grid: { stroke: ["M2.5 2.5h11v11h-11z", "M6.2 2.5v11", "M9.8 2.5v11", "M2.5 6.2h11", "M2.5 9.8h11"] },
  plus: { stroke: ["M8 3v10", "M3 8h10"] },
  duplicate: {
    stroke: [
      "M6 5.5h6.5a.5.5 0 0 1 .5.5v7a.5.5 0 0 1-.5.5H6a.5.5 0 0 1-.5-.5V6a.5.5 0 0 1 .5-.5z",
      "M10.5 5.5V3a.5.5 0 0 0-.5-.5H3.5A.5.5 0 0 0 3 3v7a.5.5 0 0 0 .5.5h2",
      "M9.25 7.5v4",
      "M7.25 9.5h4",
    ],
  },
  arrowUp: { stroke: ["M8 13V3", "M4 7l4-4 4 4"] },
  arrowDown: { stroke: ["M8 3v10", "M4 9l4 4 4-4"] },
  eye: {
    stroke: ["M1.5 8S3.9 3.5 8 3.5 14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z", "M10 8a2 2 0 1 1-4 0 2 2 0 0 1 4 0z"],
  },
  eyeOff: {
    stroke: [
      "M6.3 3.7A6.6 6.6 0 0 1 8 3.5C12.1 3.5 14.5 8 14.5 8a11 11 0 0 1-1.8 2.4",
      "M10.8 11.8a6.1 6.1 0 0 1-2.8.7C3.9 12.5 1.5 8 1.5 8a11.3 11.3 0 0 1 2.8-3.3",
      "M2.5 2.5l11 11",
    ],
  },
  lock: { stroke: ["M4.3 7.5h7.4a.8.8 0 0 1 .8.8v4.4a.8.8 0 0 1-.8.8H4.3a.8.8 0 0 1-.8-.8V8.3a.8.8 0 0 1 .8-.8z", "M5.5 7.5v-2a2.5 2.5 0 0 1 5 0v2"] },
  unlock: { stroke: ["M4.3 7.5h7.4a.8.8 0 0 1 .8.8v4.4a.8.8 0 0 1-.8.8H4.3a.8.8 0 0 1-.8-.8V8.3a.8.8 0 0 1 .8-.8z", "M5.5 7.5v-2a2.5 2.5 0 0 1 4.9-.7"] },
  play: { fill: ["M5 3.4v9.2a.5.5 0 0 0 .8.4l7-4.6a.5.5 0 0 0 0-.8l-7-4.6a.5.5 0 0 0-.8.4z"] },
  stop: { fill: ["M4.5 4h7a.5.5 0 0 1 .5.5v7a.5.5 0 0 1-.5.5h-7a.5.5 0 0 1-.5-.5v-7a.5.5 0 0 1 .5-.5z"] },
  flag: { stroke: ["M3.5 14V2.5", "M3.5 3h8.5l-2 2.75 2 2.75H3.5"] },
  chevron: { stroke: ["M6 3.5 10.5 8 6 12.5"] },
  close: { stroke: ["M4 4l8 8", "M12 4l-8 8"] },
};

export function icon(name) {
  const shape = ICONS[name];
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "editor-icon");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const [kind, paths] of [
    ["stroke", shape.stroke ?? []],
    ["fill", shape.fill ?? []],
  ]) {
    for (const d of paths) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      if (kind === "fill") path.setAttribute("class", "editor-icon-fill");
      svg.append(path);
    }
  }
  return svg;
}
