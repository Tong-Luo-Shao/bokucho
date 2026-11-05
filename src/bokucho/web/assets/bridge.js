// bridge.js -- the only module that touches `pywebview`.
//
// Resolves `window.pywebview.api` once the host signals `pywebviewready`,
// registers the `bokucho.onEvent` sink the Python EventBus pushes progress and
// conflict events into, and falls back to an in-memory dev shim so the shell
// still renders when opened in a plain browser (no pywebview host at all).

const READY_TIMEOUT_MS = 400;

function waitForApi() {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) {
      resolve(window.pywebview.api);
      return;
    }
    let settled = false;
    const onReady = () => {
      if (settled) return;
      settled = true;
      resolve(window.pywebview.api);
    };
    window.addEventListener("pywebviewready", onReady, { once: true });
    setTimeout(() => {
      if (settled) return;
      settled = true;
      window.removeEventListener("pywebviewready", onReady);
      resolve(null);
    }, READY_TIMEOUT_MS);
  });
}

const eventListeners = new Set();
window.bokucho = window.bokucho || {};
window.bokucho.onEvent = (event) => {
  for (const listener of eventListeners) listener(event);
};

export function onEvent(listener) {
  eventListeners.add(listener);
  return () => eventListeners.delete(listener);
}

export class BridgeError extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

let apiPromise = null;
function resolveApi() {
  if (apiPromise === null) {
    apiPromise = waitForApi().then((real) => real ?? devShim);
  }
  return apiPromise;
}

async function call(method, ...args) {
  const api = await resolveApi();
  const result = await api[method](...args);
  if (result && typeof result === "object" && result.error) {
    throw new BridgeError(result.error.kind, result.error.message);
  }
  return result;
}

// Every Api method is a plain RPC forward -- a Proxy avoids hand-listing the
// thirty-odd names and keeps this file free of any method-specific logic.
export const bridge = new Proxy(
  {},
  {
    get(_target, method) {
      return (...args) => call(method, ...args);
    },
  },
);

// -- Dev shim -----------------------------------------------------------
// Only reached when no pywebview host ever announces itself (a plain
// browser preview). Mirrors the Api method surface with fixed sample data so
// the layout and screens render without a real backend.

const DEV_GUI_SETTINGS = {
  locale: "zh_TW",
  theme: "dark",
  accent: "clay",
  density: "normal",
  nav_expanded: false,
  font_family: "",
  font_size: 16,
};

const DEV_UNITS = [
  { source_file: "scene01.json", id: "u1", source: "アオイ、ギルドへようこそ。", target: "", status: "untranslated", kind: "text", image: "", regions: [] },
  { source_file: "scene01.json", id: "u2", source: "アオイは魔導書を閉じた。", target: "阿奧伊闔上了魔導書。", status: "mt", kind: "text", image: "", regions: [], history: [{ target: "艾尔特合上魔导书。", status: "mt", at: "2026-09-05T08:12:00Z" }] },
  { source_file: "scene01.json", id: "u3", source: "さようなら。", target: "再見。", status: "reviewed", kind: "text", image: "", regions: [] },
  {
    source_file: "images",
    id: "img:ui/title.gal",
    source: "はじめから\nつづきから",
    target: "",
    status: "untranslated",
    kind: "image",
    image: "ui/title.gal",
    regions: [
      { box: [20, 20, 100, 40], text: "はじめから" },
      { box: [20, 60, 100, 80], text: "つづきから" },
    ],
  },
];

const DEV_GLOSSARY = [
  { source: "アオイ", target: "阿奧伊", note: "主角", origin: "project", occurrences: 42 },
  { source: "空の物語", target: "天空物語", note: "作品名", origin: "project", occurrences: 8 },
  { source: "魔導書", target: "魔導書", note: null, origin: "global", occurrences: 15 },
  { source: "ギルド", target: "公會", note: "組織", origin: "shadowed", occurrences: 27 },
];

const DEV_PROJECT = {
  root: "C:/dev/sample-project",
  name: "Sample Project (dev shim)",
  source_lang: "ja",
  target_lang: "zh-tw",
  engine: "livemaker",
};

// A picture with text, one the scan found nothing on, and one left as it is.
const DEV_IMAGES = [
  { image: "graphics/sample.png", format: "PNG", width: 32, height: 24, mode: "RGBA" },
  { image: "graphics/portrait.png", format: "PNG", width: 32, height: 24, mode: "RGBA", boxes: [] },
  { image: "graphics/title.png", format: "PNG", width: 32, height: 24, mode: "RGBA", ignored: true },
];

// The default face, and what the machine offers that draws the translations.
const DEV_FONTS = [
  { path: "C:/Windows/Fonts/msjh.ttc", name: "Microsoft JhengHei" },
  { path: "C:/Windows/Fonts/unifont-all.ttf", name: "Unifont Smooth Medium" },
];
// The pictures' list leads with the faces Bokucho ships; the release's does not.
const DEV_TYPESET_FONTS = [
  { path: "bundled:GenYoGothic2TW-R.otf", name: "GenYoGothic2 TW R" },
  { path: "bundled:GenYoMin2TW-R.otf", name: "GenYoMin2 TW R" },
  ...DEV_FONTS,
];
const DEV_TYPESET_FONT = {
  font: null,
  default: { path: "C:/Windows/Fonts/msjhbd.ttc", name: "Microsoft JhengHei Bold" },
};
const DEV_GAME_FONT = {
  font: null,
  sample: ["阿奧伊闔上了魔導書。", "阿奧伊，歡迎來到公會。"],
};
const DEV_UIF = { path: null, bundled: true };

const DEV_OCR_STATUS = {
  detector: {
    model_id: "PaddlePaddle/PP-OCRv6_medium_det_safetensors",
    revision: "4236c2b61741a259c091fd879dcc4edc339e916c",
    download_bytes: 88049188,
    cached: false,
  },
  recognizer: {
    model_id: "PaddlePaddle/PP-OCRv6_medium_rec_safetensors",
    revision: "024cad6a831de75c2c3c26e711ba8c4a82ccd24b",
    download_bytes: 77124914,
    cached: false,
  },
  eraser: {
    download_bytes: 205669692,
    cached: false,
  },
  detection_threshold: 0.9,
  recognition_threshold: 0.9,
  batch_size: 8,
  recommended_batch_size: 8,
  batch_size_mode: "auto",
  runtime_cached: true,
  runtime_download_bytes: 2700000000,
};

const DEV_PROVIDER_CATALOG = {
  providers: [
    {
      key: "gemini",
      label: "Gemini",
      base_url: "/dev-api/gemini",
      api_key_env: "GEMINI_API_KEY",
    },
    {
      key: "claude",
      label: "Claude",
      base_url: "/dev-api/claude",
      api_key_env: "ANTHROPIC_API_KEY",
    },
    {
      key: "openai",
      label: "OpenAI",
      base_url: "/dev-api/openai",
      api_key_env: "OPENAI_API_KEY",
    },
  ],
  interfaces: [
    {
      id: "dev-gemini",
      name: "Gemini production",
      provider: "gemini",
      model: "gemini-2.5-flash",
      base_url: "/dev-api/gemini",
      api_key_env: "GEMINI_API_KEY",
      batch_size: 20,
      rpm: 15,
      tpm: 250000,
      rpd: 1500,
      temperature: 0.2,
      thinking_level: "medium",
      credential_count: 1,
    },
    {
      id: "dev-claude",
      name: "Claude review",
      provider: "claude",
      model: "claude-sonnet-4-6",
      base_url: "/dev-api/claude",
      api_key_env: "ANTHROPIC_API_KEY",
      batch_size: 12,
      rpm: 10,
      tpm: 100000,
      rpd: 1000,
      temperature: null,
      thinking_level: "high",
      credential_count: 1,
    },
  ],
  active_interface_id: "dev-gemini",
};

const resolved = (value) => Promise.resolve(value);
const findDevUnit = (sourceFile, id) =>
  DEV_UNITS.find((row) => row.source_file === sourceFile && row.id === id);
const devSpans = (source) =>
  DEV_GLOSSARY.flatMap((entry) => {
    const spans = [];
    for (let at = source.indexOf(entry.source); at !== -1; at = source.indexOf(entry.source, at + 1)) {
      spans.push([at, at + entry.source.length, entry.target]);
    }
    return spans;
  }).sort((a, b) => a[0] - b[0]);
// The backend reads variables with the engine's own markup; %name% and
// <VAR ...> cover the sample lines here.
const devCategory = (unit) =>
  /%[^%\s]+%|<VAR [^<>]+>/.test(unit.source)
    ? "variables"
    : ({ value: "values", image: "images" }[unit.kind] ?? "story");
const devUnitMatches = (unit, status, query, scope, category, sourceLang) => {
  if (status && unit.status !== status) return false;
  if (category && devCategory(unit) !== category) return false;
  // The preview corpus contains only Japanese source text.
  if (sourceLang && sourceLang !== "und" && sourceLang.split("-")[0] !== "ja") return false;
  const needle = query?.toLocaleLowerCase();
  if (!needle) return true;
  return unit[scope].toLocaleLowerCase().includes(needle);
};
// Memory offers what other lines in the same file settled on, which is enough
// shape for the editor: a target, where it came from, and how often.
const devSuggestions = (unit) =>
  DEV_UNITS.filter(
    (row) => row.source_file === unit.source_file && row.id !== unit.id && row.target,
  ).map((row) => ({ target: row.target, origin: "memory", status: row.status, count: 1 }));
const devImageData = () => {
  const canvas = document.createElement("canvas");
  canvas.width = 32;
  canvas.height = 24;
  const context = canvas.getContext("2d");
  context.fillStyle = "#f8fafc";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#155eef";
  context.fillRect(4, 4, 24, 16);
  context.fillStyle = "#facc15";
  context.fillRect(10, 8, 12, 8);
  return canvas.toDataURL("image/png").split(",")[1];
};
const devEditorState = () => ({
  path: "graphics/sample.png",
  width: 32,
  height: 24,
  mode: "RGBA",
  format: "PNG",
  has_alpha: true,
  editable: true,
  source_fingerprint: "dev-source",
  output_fingerprint: null,
  palette: [],
  text_layer: {
    source: "SAMPLE",
    target: "",
    region: null,
    regions: [{ box: [4, 6, 28, 18], text: "SAMPLE" }],
    style: {
      font_family: "",
      font_size: 8,
      fill: "#FFFFFFFF",
      stroke: "#FF000000",
      stroke_width: 1,
      background: "#00000000",
      alignment: "center",
    },
  },
  active_frame: 0,
  output_frame: 0,
  onion_skin: { enabled: false, previous_frames: 1, next_frames: 1, opacity: 0.35 },
  frames: [
    {
      id: "frame-1",
      duration_ms: 120,
      layers: [
        {
          id: "layer-1",
          name: "Layer 1",
          data: devImageData(),
          visible: true,
          opacity: 1,
          locked: false,
        },
      ],
    },
  ],
});

const devShim = {
  minimize_window: () => resolved({ minimized: true }),
  toggle_maximize_window: () => resolved({ maximized: false }),
  begin_window_drag: () => resolved({ maximized: false }),
  begin_window_resize: () => resolved({ resizing: true }),
  close_window: () => resolved({ closed: true }),
  forget_project: () => resolved([]),
  recent_projects: () =>
    resolved([
      {
        path: DEV_PROJECT.root,
        name: DEV_PROJECT.name,
        engine: DEV_PROJECT.engine,
        source_lang: DEV_PROJECT.source_lang,
        target_lang: DEV_PROJECT.target_lang,
        last_opened: new Date().toISOString(),
        available: true,
      },
    ]),
  list_engines: () => resolved([
    "livemaker", "renpy", "rpgmaker-mv", "rpgmaker-mz", "rpgmaker-vx-ace",
    "rpgmaker-xp", "rpgmaker-vx",
  ]),
  detect_engine: () => resolved({ results: [] }),
  choose_directory: () => resolved(null),
  open_release_folder: (path) => resolved({ opened: path }),
  create_project: () => resolved({ root: DEV_PROJECT.root, engine: DEV_PROJECT.engine }),
  open_project: () => resolved({ ...DEV_PROJECT }),
  get_catalog: () => resolved({}),
  gui_settings: () => resolved({ ...DEV_GUI_SETTINGS }),
  // Segoe UI Variable is a name the browser does not draw by, to show it dropped.
  font_families: () => resolved({
    families: [
      "Bahnschrift", "Cascadia Code", "Microsoft JhengHei UI", "Microsoft YaHei UI",
      "Segoe UI", "Segoe UI Variable", "Yu Gothic UI",
    ],
  }),
  save_gui_settings: (theme, accent, density, locale, nav_expanded, font_family, font_size) => {
    Object.assign(DEV_GUI_SETTINGS, {
      theme, accent, density, locale, nav_expanded, font_family, font_size,
    });
    return resolved({ ...DEV_GUI_SETTINGS });
  },
  // One real check over the shim's units, so the screens that read quality
  // have something that moves when a target does.
  quality: () =>
    resolved({
      status_counts: { untranslated: 2, mt: 1, reviewed: 1 },
      issues: DEV_UNITS.filter((unit) => unit.target && unit.target === unit.source).map(
        (unit) => ({
          file: unit.source_file,
          unit_id: unit.id,
          check: "echo",
          message: "Target is identical to the source.",
        }),
      ),
      passed: true,
    }),
  unit_rows: (status, query, offset = 0, limit = 200, scope = "source", category = null, sourceLang = null) => {
    const matched = DEV_UNITS.filter((unit) =>
      devUnitMatches(unit, status, query, scope, category, sourceLang),
    );
    return resolved({
      total: matched.length,
      rows: matched.slice(Math.max(0, offset), Math.max(0, offset) + Math.max(0, limit)),
    });
  },
  unit_languages: () => resolved(["ja"]),
  unit_counts: () => {
    const statusCounts = {
      untranslated: 0,
      mt: 0,
      reviewed: 0,
      ignored: 0,
    };
    const byFile = new Map();
    for (const unit of DEV_UNITS) {
      statusCounts[unit.status] += 1;
      if (!byFile.has(unit.source_file)) {
        byFile.set(unit.source_file, {
          untranslated: 0,
          mt: 0,
          reviewed: 0,
          ignored: 0,
        });
      }
      byFile.get(unit.source_file)[unit.status] += 1;
    }
    const files = [...byFile.entries()].map(([sourceFile, counts]) => ({
      source_file: sourceFile,
      total: Object.values(counts).reduce((a, b) => a + b, 0),
      status_counts: counts,
    }));
    const imageUnits = DEV_UNITS.filter((unit) => unit.kind === "image").length;
    return resolved({
      total: DEV_UNITS.length,
      status_counts: statusCounts,
      files,
      image_units: imageUnits,
    });
  },
  unit_detail: (sourceFile, id) => {
    const unit = findDevUnit(sourceFile, id);
    return resolved({
      ...unit,
      suggestions: devSuggestions(unit),
      history: unit.history || [],
      spans: devSpans(unit.source),
    });
  },
  save_draft: (sourceFile, id, target) => {
    const unit = findDevUnit(sourceFile, id);
    if (unit) unit.target = target;
    return resolved({ ...unit });
  },
  begin_edit: () => resolved({ status: "ok" }),
  accept_disk: () => resolved({ status: "ok" }),
  keep_draft: () => resolved({ status: "ok" }),
  mark_reviewed: (sourceFile, id) => {
    const unit = findDevUnit(sourceFile, id);
    if (unit) unit.status = "reviewed";
    return resolved({ ...unit });
  },
  set_unit_status: (sourceFile, id, status) => {
    const unit = findDevUnit(sourceFile, id);
    if (unit && status === "ignored") {
      unit.history = [...(unit.history ?? []), { target: unit.target, status: unit.status, at: new Date().toISOString() }];
      unit.target = "";
    } else if (unit?.status === "ignored" && !unit.target) {
      unit.target = [...(unit.history ?? [])].reverse().find((entry) => entry.target)?.target ?? "";
    }
    if (unit) unit.status = status;
    return resolved({ ...unit });
  },
  next_unit: (sourceFile, id, status, query, scope = "source", category = null, sourceLang = null) => {
    const index = DEV_UNITS.findIndex(
      (unit) => unit.source_file === sourceFile && unit.id === id,
    );
    const next = DEV_UNITS.slice(index + 1).find((unit) =>
      devUnitMatches(unit, status, query, scope, category, sourceLang),
    );
    return resolved(next ? { ...next } : null);
  },
  import_mapping: () => resolved({ imported: 0, unmatched: 0 }),
  glossary_entries: () => resolved(DEV_GLOSSARY.map((entry) => ({ ...entry }))),
  save_glossary_entry: (source, target, note, scope) => {
    const existing = DEV_GLOSSARY.find((entry) => entry.source === source);
    if (existing) {
      existing.target = target;
      existing.note = note ?? null;
      return resolved({ created: false });
    }
    DEV_GLOSSARY.push({
      source,
      target,
      note: note ?? null,
      origin: scope === "global" ? "global" : "project",
      occurrences: 0,
    });
    return resolved({ created: true });
  },
  delete_glossary_entry: (source, scope) => {
    const index = DEV_GLOSSARY.findIndex(
      (entry) =>
        entry.source === source &&
        (scope === "global") === (entry.origin === "global" || entry.origin === "shadowed"),
    );
    if (index !== -1) DEV_GLOSSARY.splice(index, 1);
    return resolved({ deleted: index !== -1 });
  },
  image_inventory: () => resolved({ entries: structuredClone(DEV_IMAGES) }),
  ocr_status: () => resolved(structuredClone(DEV_OCR_STATUS)),
  save_ocr_configuration: (batchSize, detectionThreshold, recognitionThreshold) => {
    DEV_OCR_STATUS.batch_size = batchSize || DEV_OCR_STATUS.recommended_batch_size;
    DEV_OCR_STATUS.batch_size_mode = batchSize ? "custom" : "auto";
    DEV_OCR_STATUS.detection_threshold = detectionThreshold;
    DEV_OCR_STATUS.recognition_threshold = recognitionThreshold;
    return resolved(structuredClone(DEV_OCR_STATUS));
  },
  image_data: () => resolved({ data: devImageData() }),
  image_editor_state: () => resolved(devEditorState()),
  save_image_editor_state: () =>
    resolved({
      output_path: "out/graphics/sample.png",
      output_fingerprint: "dev-output",
    }),
  start_job: (name) => resolved({ status: "started", job: name }),
  cancel_job: () => resolved({ status: "cancelling" }),
  pause_job: () => resolved({ status: "paused" }),
  resume_job: () => resolved({ status: "running" }),
  get_settings: () => resolved({}),
  save_project_settings: () => resolved({}),
  save_translator_settings: () => resolved({}),
  provider_interfaces: () => resolved(structuredClone(DEV_PROVIDER_CATALOG)),
  provider_interface_keys: () => resolved({ api_keys: ["dev-api-key"] }),
  activate_provider_interface: (interfaceId) => {
    DEV_PROVIDER_CATALOG.active_interface_id = interfaceId;
    return resolved({ active_interface_id: interfaceId });
  },
  save_provider_interface: () => resolved({ interface: DEV_PROVIDER_CATALOG.interfaces[0] }),
  update_provider_interface_parameters: () =>
    resolved({ interface: DEV_PROVIDER_CATALOG.interfaces[0] }),
  delete_provider_interface: () => resolved({ deleted: true }),
  discover_provider_models: () =>
    resolved({ models: ["gemini-2.5-flash", "gemini-2.5-pro"] }),
  test_provider_interface: () => resolved({ ok: true, models: 2 }),
  recheck_preview: () => resolved({ settled: 2, sent_back: 1 }),
  brackets_preview: () => resolved({ fixed: 3 }),
  values_preview: () => resolved({ fixed: 2 }),
  typeset_font: () => resolved(structuredClone(DEV_TYPESET_FONT)),
  typeset_fonts: () => resolved({ fonts: structuredClone(DEV_TYPESET_FONTS) }),
  save_typeset_font: (path) => {
    DEV_TYPESET_FONT.font = path
      ? DEV_TYPESET_FONTS.find((font) => font.path === path) ?? null
      : null;
    return resolved(structuredClone(DEV_TYPESET_FONT));
  },
  // A browser preview has no font files to hand over; the editor letters the
  // layer by the name it gives instead.
  typeset_font_data: (path) =>
    Promise.reject(new BridgeError("validation", `No font file in a preview: ${path}`)),
  game_font: () => resolved(structuredClone(DEV_GAME_FONT)),
  game_fonts: () => resolved({ fonts: structuredClone(DEV_FONTS) }),
  save_game_font: (path) => {
    DEV_GAME_FONT.font = DEV_FONTS.find((font) => font.path === path) ?? null;
    return resolved(structuredClone(DEV_GAME_FONT));
  },
  uif_dll: () => resolved(structuredClone(DEV_UIF)),
  save_uif_dll: (path) => {
    DEV_UIF.path = path || null;
    return resolved(structuredClone(DEV_UIF));
  },
  choose_file: () => resolved("C:/tools/uif/winmm.dll"),
  set_images_ignored: (paths, ignored) => {
    for (const entry of DEV_IMAGES.filter((row) => paths.includes(row.image))) {
      if (ignored) entry.ignored = true;
      else delete entry.ignored;
    }
    return resolved({ changed: paths.length });
  },
  prompt_presets: () =>
    resolved({
      presets: [
        {
          id: "builtin:general",
          name: "General",
          system: "You are a professional game translator...",
          user: "{entries_json}",
          builtin: true,
        },
        {
          id: "builtin:visual-novel",
          name: "Visual novel",
          system: "You are a professional visual-novel translator...",
          user: "{entries_json}",
          builtin: true,
        },
        {
          id: "user:house",
          name: "House style (dev shim)",
          system: "House rules...",
          user: "{entries_json}",
          builtin: false,
        },
      ],
      selected_id: "builtin:general",
    }),
  save_prompt_preset: () => resolved({ presets: [] }),
  delete_prompt_preset: () => resolved({ presets: [] }),
  select_prompt_preset: (id) => resolved({ selected_id: id }),
};
