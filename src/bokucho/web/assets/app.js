// app.js -- hash router, app shell wiring (sidebar, job bar, error banner,
// conflict dialog), and the shared context every screen mounts against.

import { bridge, onEvent } from "./bridge.js";
import { createBoundedLog } from "./bounded_log.js";
import { hideDialog, showDialog, trapFocus } from "./dialog.js";
import { loadCatalog, t } from "./i18n.js";
import { createScreenHost } from "./screen_host.js";
import { createTranslationRun, updateTranslationRun } from "./translation_run.js";
import * as launcherScreen from "./screens/launcher.js";
import * as overviewScreen from "./screens/overview.js";
import * as translationDashboardScreen from "./screens/translation_dashboard.js";
import * as stringsScreen from "./screens/strings.js";
import * as glossaryScreen from "./screens/glossary.js";
import * as imagesScreen from "./screens/images.js";
import * as qaScreen from "./screens/qa.js";
import * as releaseScreen from "./screens/release.js";
import * as modelSettingsScreen from "./screens/model_settings.js";
import * as ocrSettingsScreen from "./screens/ocr_settings.js";
import * as promptsScreen from "./screens/prompts.js";
import * as settingsScreen from "./screens/settings.js";
import { openCreateProjectDialog } from "./screens/create_project.js";

const WINDOW_DRAG_THRESHOLD_PX = 4;

// The rail carries the eight screens the work runs through, in the order the
// work runs through them. Everything else is reached from the screen it
// belongs to: the dashboard from the run, prompts and the OCR model from
// settings.
const RAIL = [
  ["overview", "Overview", "◎"],
  ["strings", "Strings", "≡"],
  ["glossary", "Glossary", "A"],
  ["images", "Images", "▣"],
  ["qa", "Quality", "!"],
  ["release", "Release", "↑"],
  ["model", "Model", "◇"],
  ["settings", "Settings", "⚙"],
];

const UI_LANG = { zh_TW: "zh-Hant", en: "en" };

const APPEARANCE_DEFAULTS = {
  theme: "dark",
  accent: "clay",
  density: "normal",
  navExpanded: false,
  fontFamily: "",
  fontSize: 16,
};

const ROUTES = {
  overview: overviewScreen,
  "translation-dashboard": translationDashboardScreen,
  strings: stringsScreen,
  glossary: glossaryScreen,
  images: imagesScreen,
  qa: qaScreen,
  release: releaseScreen,
  model: modelSettingsScreen,
  "ocr-settings": ocrSettingsScreen,
  prompts: promptsScreen,
  settings: settingsScreen,
};

const dom = {
  windowResizeZones: document.querySelectorAll("[data-resize-edge]"),
  windowDragRegion: document.getElementById("window-drag-region"),
  windowControls: document.getElementById("window-controls"),
  windowMinimizeButton: document.getElementById("window-minimize-button"),
  windowMaximizeButton: document.getElementById("window-maximize-button"),
  windowCloseButton: document.getElementById("window-close-button"),
  projectTitle: document.getElementById("project-title"),
  jobStatusButton: document.getElementById("job-status"),
  reviewedPercentage: document.getElementById("reviewed-percentage"),
  themeToggle: document.getElementById("theme-toggle"),
  errorBanner: document.getElementById("error-banner"),
  errorMessage: document.getElementById("error-message"),
  errorDetailsButton: document.getElementById("error-details-button"),
  errorDismissButton: document.getElementById("error-dismiss-button"),
  errorDetails: document.getElementById("error-details"),
  launcherPage: document.getElementById("launcher-page"),
  workspace: document.getElementById("workspace"),
  sidebar: document.getElementById("sidebar"),
  page: document.getElementById("page"),
  pageHeading: document.getElementById("page-heading"),
  jobBar: document.getElementById("job-bar"),
  jobStatus: document.getElementById("job-status"),
  jobAnnouncer: document.getElementById("job-announcer"),
  jobProgress: document.getElementById("job-progress"),
  jobProgressDetail: document.getElementById("job-progress-detail"),
  jobPercentage: document.getElementById("job-percentage"),
  jobCancelButton: document.getElementById("job-cancel-button"),
  jobHistoryButton: document.getElementById("job-history-button"),
  jobLog: document.getElementById("job-log"),
  conflictDialog: document.getElementById("conflict-dialog"),
  conflictPanel: document.getElementById("conflict-panel"),
  conflictError: document.getElementById("conflict-error"),
  conflictDescription: document.getElementById("conflict-description"),
  conflictIdentity: document.getElementById("conflict-identity"),
  conflictAcceptDisk: document.getElementById("conflict-accept-disk"),
  conflictKeepDraft: document.getElementById("conflict-keep-draft"),
};

const appState = { project: null, qaReport: null, translationRun: null };
const screenHost = createScreenHost(dom.page, {
  persistentRoutes: new Set(["strings"]),
});

// A plain, localized line for the failures a direct action can raise, keyed by
// the AppError.code the bridge carries back. The banner shows this instead of
// the raw English exception text; that text still reaches Details for anyone
// diagnosing the failure. Job-run failures arrive keyed by the job name, not a
// code, so they keep their own message untouched.
const ERROR_HEADLINES = {
  validation: "The action could not be completed.",
  invalid_project: "Open a folder that holds a Bokucho project.",
  missing_credential: "An API key is missing.",
  missing_dependency: "A required component is not ready yet.",
  multi_frame_image: "This image has more than one frame and cannot be opened here.",
  release_blocked: "Finish the remaining units to build the release.",
  job_busy: "Wait for the running task to finish.",
  application_error: "Something went wrong.",
};

function showError(error) {
  const raw = error && error.message ? error.message : String(error);
  const kind = error && error.kind ? error.kind : "";
  const headline = ERROR_HEADLINES[kind] ? t("ErrorBanner", ERROR_HEADLINES[kind]) : raw;
  dom.errorMessage.textContent = headline;
  // The raw text is only worth a Details button when it says more than the
  // headline already does.
  const detail = headline === raw ? "" : raw;
  dom.errorDetails.textContent = detail;
  dom.errorDetails.hidden = true;
  // Both buttons ship empty in index.html, same as the conflict dialog's.
  dom.errorDetailsButton.textContent = t("ErrorBanner", "Details");
  dom.errorDismissButton.textContent = t("ErrorBanner", "Dismiss");
  dom.errorDetailsButton.hidden = !detail;
  dom.errorBanner.hidden = false;
}

function setMaximizedState(maximized) {
  document.documentElement.dataset.maximized = String(maximized);
  dom.windowMaximizeButton.dataset.maximized = String(maximized);
  const label = t("MainWindow", maximized ? "Restore" : "Maximize");
  dom.windowMaximizeButton.setAttribute("aria-label", label);
  dom.windowMaximizeButton.title = label;
}

function localizeWindowControls() {
  const minimize = t("MainWindow", "Minimize");
  const close = t("MainWindow", "Close");
  dom.windowControls.setAttribute("aria-label", t("MainWindow", "Window controls"));
  dom.windowMinimizeButton.setAttribute("aria-label", minimize);
  dom.windowMinimizeButton.title = minimize;
  dom.windowCloseButton.setAttribute("aria-label", close);
  dom.windowCloseButton.title = close;
  setMaximizedState(dom.windowMaximizeButton.dataset.maximized === "true");
}

async function toggleMaximizeWindow() {
  try {
    const state = await bridge.toggle_maximize_window();
    setMaximizedState(Boolean(state.maximized));
  } catch (err) {
    showError(err);
  }
}

function prepareWindowDrag(event) {
  if (event.button !== 0) return;

  const startX = event.screenX;
  const startY = event.screenY;

  function stopTracking() {
    window.removeEventListener("mousemove", trackPointer);
    window.removeEventListener("mouseup", stopTracking);
  }

  function trackPointer(moveEvent) {
    const distance = Math.hypot(
      moveEvent.screenX - startX,
      moveEvent.screenY - startY,
    );
    if (distance < WINDOW_DRAG_THRESHOLD_PX) return;

    stopTracking();
    moveEvent.preventDefault();
    bridge.begin_window_drag()
      .then((state) => setMaximizedState(Boolean(state.maximized)))
      .catch((err) => showError(err));
  }

  window.addEventListener("mousemove", trackPointer);
  window.addEventListener("mouseup", stopTracking);
}

function beginWindowResize(event) {
  if (event.button !== 0) return;

  const edge = event.currentTarget.dataset.resizeEdge;
  event.preventDefault();
  bridge.begin_window_resize(edge).catch((err) => showError(err));
}

dom.windowMinimizeButton.addEventListener("click", async () => {
  try {
    await bridge.minimize_window();
  } catch (err) {
    showError(err);
  }
});
for (const zone of dom.windowResizeZones) {
  zone.addEventListener("mousedown", beginWindowResize);
}
dom.windowDragRegion.addEventListener("mousedown", prepareWindowDrag);
dom.windowMaximizeButton.addEventListener("click", toggleMaximizeWindow);
dom.windowCloseButton.addEventListener("click", async () => {
  try {
    await bridge.close_window();
  } catch (err) {
    showError(err);
  }
});
dom.windowDragRegion.addEventListener("dblclick", toggleMaximizeWindow);

dom.errorDetailsButton.addEventListener("click", () => {
  dom.errorDetails.hidden = !dom.errorDetails.hidden;
});
dom.errorDismissButton.addEventListener("click", () => {
  dom.errorBanner.hidden = true;
});

// -- appearance ------------------------------------------------------------

// Theme, accent and row height are attributes on the root element because the
// window carries switches for all three, and a media query cannot be switched.
const appearance = { ...APPEARANCE_DEFAULTS };
let uiLocale = "zh_TW";

function applyAppearance() {
  const root = document.documentElement;
  root.dataset.theme = appearance.theme;
  root.dataset.accent = appearance.accent;
  root.dataset.density = appearance.density;
  root.dataset.navExpanded = String(appearance.navExpanded);
  root.style.setProperty("--font-scale", String(appearance.fontSize / 13));
  if (appearance.fontFamily) {
    root.style.setProperty("--font-family-custom", `${JSON.stringify(appearance.fontFamily)},`);
  } else {
    root.style.removeProperty("--font-family-custom");
  }
  const label = t(
    "MainWindow",
    appearance.theme === "dark"
      ? "Switch to the light theme"
      : "Switch to the dark theme",
  );
  dom.themeToggle.title = label;
  dom.themeToggle.setAttribute("aria-label", label);
  updateNavToggle();
}

function storeAppearance() {
  return bridge
    .save_gui_settings(
      appearance.theme,
      appearance.accent,
      appearance.density,
      uiLocale,
      appearance.navExpanded,
      appearance.fontFamily,
      appearance.fontSize,
    )
    .catch((err) => showError(err));
}

function setAppearance(patch) {
  Object.assign(appearance, patch);
  applyAppearance();
  return storeAppearance();
}

dom.themeToggle.addEventListener("click", () => {
  setAppearance({ theme: appearance.theme === "dark" ? "light" : "dark" });
});

// Quality is not a run anyone starts. The checks read a unit's source and
// target and nothing else, so the report is recomputed after every edit and
// every job, and whatever is on screen is what the project says right now.
const qualityListeners = new Set();
let qualityRun = null;
let qualityStale = false;

// One check at a time: a large project takes seconds to check, and the edits
// made while one runs are covered by a single run after it, not one each.
function refreshQuality() {
  if (qualityRun !== null) {
    qualityStale = true;
    return qualityRun;
  }
  qualityRun = (async () => {
    try {
      do {
        qualityStale = false;
        await checkQuality();
      } while (qualityStale);
    } finally {
      qualityRun = null;
    }
  })();
  return qualityRun;
}

async function checkQuality() {
  if (appState.project === null) {
    appState.qaReport = null;
    return;
  }
  try {
    appState.qaReport = await bridge.quality();
  } catch {
    // A project with nothing extracted yet has nothing to check; the screens
    // already say so from a null report.
    appState.qaReport = null;
  }
  for (const listener of qualityListeners) listener(appState.qaReport);
}

function onQuality(listener) {
  qualityListeners.add(listener);
  return () => qualityListeners.delete(listener);
}

// Its own listener rather than a branch in the job bar's: the job bar answers
// for one run's ownership, and a job that ended any way at all -- succeeded,
// failed, cancelled -- saved the batches it did finish.
onEvent(function recheckAfterAJobEnds(event) {
  if (["succeeded", "failed", "cancelled"].includes(event.type)) refreshQuality();
});

// A LiveMaker game's values are read beside the GUI; the checks on them join
// the report once that reading lands.
onEvent(function recheckOnceValuesAreRead(event) {
  if (event.type === "values-analysed") refreshQuality();
});

// Another program changed a string file; the report has to read it again.
onEvent(function recheckAfterAnOutsideChange(event) {
  if (event.type === "strings-changed") refreshQuality();
});

function refreshReviewedPercentage() {
  if (appState.project === null) {
    dom.reviewedPercentage.textContent = "";
    return;
  }
  bridge
    .unit_counts()
    .then((counts) => {
      const total = counts.total ?? 0;
      const statuses = counts.status_counts ?? {};
      const reviewed = statuses.reviewed ?? 0;
      const percent = total ? Math.round((reviewed / total) * 100) : 0;
      dom.reviewedPercentage.textContent = t("MainWindow", "Reviewed {percent}%", {
        percent,
      });
    })
    .catch((err) => showError(err));
}

// Marking a line reviewed is an edit, and every edit reads the quality report again.
onQuality(refreshReviewedPercentage);

// -- job bar -------------------------------------------------------------

let jobExpanded = false;
const JOB_DETAIL_LIMIT = 300;
let activeJobOwner = null;
// Set while the running job sits paused: a batch already sent still reports
// progress, and it must not write over the bar's "Paused".
let jobPaused = false;
const pendingProgress = new Map();
let progressFrameId = null;
const jobDetails = createBoundedLog();
const diagnosticLines = jobDetails.lines;
const diagnosticOrder = [];
const liveProgress = new Map();
let nextDetailOrder = 0;
let composedOmitted = 0;

function nextOrder() {
  nextDetailOrder += 1;
  return nextDetailOrder;
}

function progressKey(event) {
  return `${event.job ?? ""}:${event.phase ?? event.message ?? ""}`;
}

function composedJobDetails() {
  const diagnostics = diagnosticLines();
  const entries = diagnostics.map((line, index) => ({
    line,
    order: diagnosticOrder[index],
  }));
  entries.push(...liveProgress.values());
  entries.sort((left, right) => left.order - right.order);

  composedOmitted = Math.max(entries.length - JOB_DETAIL_LIMIT, 0);
  return entries.slice(-JOB_DETAIL_LIMIT).map(({ line }) => line);
}

jobDetails.lines = composedJobDetails;

function setJobBarState(state) {
  dom.jobBar.className = `job-${state}`;
}

// The status button's text changes with every progress tick, so the screen
// reader is told only when the run changes state. A failure is spoken by the
// error banner, which every failing path raises.
function announceJobState() {
  dom.jobAnnouncer.textContent = dom.jobStatus.textContent;
}

// Screens that show the running job's details as well as the bar does.
const jobLogListeners = new Set();

function jobLogLines() {
  const lines = jobDetails.lines();
  let omitted = jobDetails.dropped + composedOmitted;
  if (omitted > 0) {
    const markerOverflow = Math.max(lines.length + 1 - JOB_DETAIL_LIMIT, 0);
    lines.splice(0, markerOverflow);
    omitted += markerOverflow;
    lines.unshift(
      t("JobBar", "{count} earlier details omitted", { count: omitted }),
    );
  }
  return lines;
}

function renderJobLog() {
  for (const listener of jobLogListeners) listener();
  if (!jobExpanded) return;
  dom.jobLog.textContent = jobLogLines().join("\n");
  dom.jobLog.scrollTop = dom.jobLog.scrollHeight;
}

function appendLog(line) {
  if (!line) return;
  jobDetails.append(line);
  diagnosticOrder.push(nextOrder());
  const overflow = diagnosticOrder.length - diagnosticLines().length;
  if (overflow > 0) diagnosticOrder.splice(0, overflow);
  renderJobLog();
}

function updateProgressLine(key, line) {
  if (!line) return;
  liveProgress.set(key, { line, order: nextOrder() });
  renderJobLog();
}

jobDetails.upsert = updateProgressLine;

function setJobExpanded(expanded) {
  jobExpanded = expanded;
  dom.jobLog.hidden = !expanded;
  if (expanded) renderJobLog();
  dom.jobHistoryButton.textContent = expanded
    ? t("JobBar", "Hide details")
    : t("JobBar", "Job history");
}

function translateJobMessage(message, job) {
  if (message === "translate.started") {
    return t("JobBar", "Preparing translation");
  }
  if (message === "translate.batch.completed") {
    return t("JobBar", "Translated batch");
  }
  if (message === "translate.batch.saved") {
    return t("JobBar", "Saved translations");
  }
  if (message === "engine.extract.file.completed") {
    return t("JobBar", "Extracted");
  }
  if (message === "recheck.file.completed") {
    return t("JobBar", "Rechecked");
  }
  if (message === "engine.extract.file.skipped") {
    return t("JobBar", "Skipped, no readable text");
  }
  if (message === "engine.inject.file.completed") {
    return t("JobBar", "Written back");
  }
  if (message === "engine.font.script.completed") {
    return t("JobBar", "Font applied");
  }
  if (message === "engine.repack.progress") {
    return t("JobBar", "Repacking");
  }
  if (message === "engine.unpack.progress") {
    return t("JobBar", "Unpacking");
  }
  if (message === "engine.unpack.entry.skipped") {
    return t("JobBar", "Skipped, cannot be unpacked");
  }
  if (message === "Downloading CUDA OCR runtime") {
    return t("JobBar", "Downloading CUDA OCR runtime");
  }
  if (message === "Installing CUDA OCR runtime") {
    return t("JobBar", "Installing CUDA OCR runtime");
  }
  if (message === "Downloading OCR model") {
    return t("JobBar", "Downloading OCR model");
  }
  if (message === "Downloading the image eraser") {
    return t("JobBar", "Downloading the image eraser");
  }
  if (message === "Loading OCR model") {
    return t("JobBar", "Loading OCR model");
  }
  if (message === "OCR runtime verified") {
    return t("JobBar", "OCR runtime verified");
  }
  if (message === "images.scan.batch.reading") {
    return t("JobBar", "Reading image text");
  }
  if (message === "images.scan.item.completed") {
    return job === "images-extract"
      ? t("JobBar", "Extracting images")
      : t("JobBar", "Read image text");
  }
  return message ?? "";
}

function formatMegabyteProgress(event) {
  if (event.phase?.endsWith("-download") && event.total !== null && event.total !== undefined) {
    const locale = uiLocale.replace("_", "-");
    const format = new Intl.NumberFormat(locale, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    const completed = format.format((event.current ?? 0) / 1_000_000);
    const total = format.format(event.total / 1_000_000);
    return t("JobBar", "{completed} / {total} MB", { completed, total });
  }
  return "";
}

function clearProgressDetail() {
  dom.jobProgressDetail.textContent = "";
  dom.jobProgressDetail.hidden = true;
}

function applyProgress(event) {
  if (event.total === null || event.total === undefined) {
    dom.jobProgress.removeAttribute("max");
    dom.jobPercentage.textContent = "";
  } else {
    const total = Math.max(event.total, 1);
    dom.jobProgress.max = String(total);
    dom.jobProgress.value = String(event.current ?? 0);
    dom.jobPercentage.textContent = `${Math.round(((event.current ?? 0) / total) * 100)}%`;
  }
  const message = translateJobMessage(event.message, event.job);
  if (message && !jobPaused) dom.jobStatus.textContent = message;
  const megabyteProgress = formatMegabyteProgress(event);
  dom.jobProgressDetail.textContent = megabyteProgress;
  dom.jobProgressDetail.hidden = !megabyteProgress;
  const detail = megabyteProgress || event.detail || "";
  const suffix = detail ? ` [${detail}]` : "";
  // Progress stays live per phase; the bounded diagnostic log remains append-only.
  jobDetails.upsert(progressKey(event), `${message}${suffix}`);
}

// Resolves to whether the job started. A refusal is told on the job bar, and a
// screen that describes the run it started has none to describe.
async function startJob(name, options) {
  if (activeJobOwner !== null) {
    // The backend refuses a second job too; saying so here is what stops the
    // click from reading as a button that does nothing.
    showError({
      message: t("JobBar", "Another operation is already running: {name}", {
        name: activeJobOwner.name,
      }),
      kind: "job_busy",
    });
    return false;
  }
  const owner = { name };
  activeJobOwner = owner;
  jobPaused = false;
  if (name === "translate" && typeof appState !== "undefined") {
    appState.translationRun = createTranslationRun();
  }
  jobDetails.clear();
  diagnosticOrder.length = 0;
  liveProgress.clear();
  nextDetailOrder = 0;
  renderJobLog();
  dom.jobProgress.removeAttribute("max");
  dom.jobPercentage.textContent = "";
  clearProgressDetail();
  dom.jobCancelButton.disabled = false;
  setJobBarState("running");
  dom.jobStatus.textContent = t("JobBar", "Running: {name}", { name });
  announceJobState();
  appendLog(`START ${name}`);
  try {
    // Options travel as the job's keyword arguments; none is null on the way over.
    await bridge.start_job(name, options);
    return true;
  } catch (err) {
    if (activeJobOwner !== owner) return false;
    activeJobOwner = null;
    if (name === "translate" && typeof appState !== "undefined") {
      appState.translationRun = updateTranslationRun(
        appState.translationRun,
        { type: "failed", job: name },
      );
    }
    const message = err && err.message ? err.message : String(err);
    setJobBarState("failed");
    dom.jobCancelButton.disabled = true;
    dom.jobStatus.textContent = t("JobBar", "Failed: {message}", { message });
    showError(err);
    return false;
  }
  return true;
}

async function cancelJob() {
  setJobBarState("cancelling");
  dom.jobStatus.textContent = t("JobBar", "Cancelling...");
  announceJobState();
  dom.jobCancelButton.disabled = true;
  appendLog("CANCELLING");
  try {
    await bridge.cancel_job();
  } catch (err) {
    showError(err);
  }
}

async function pauseJob() {
  try {
    await bridge.pause_job();
  } catch (err) {
    showError(err);
  }
}

async function resumeJob() {
  try {
    await bridge.resume_job();
  } catch (err) {
    showError(err);
  }
}

dom.jobCancelButton.addEventListener("click", cancelJob);
dom.jobHistoryButton.addEventListener("click", () => setJobExpanded(!jobExpanded));

// -- conflict dialog -------------------------------------------------------

function showConflict(event) {
  dom.conflictDescription.textContent = t(
    "ConflictDialog",
    "The file changed outside Bokucho. Choose which draft to keep.",
  );
  dom.conflictIdentity.textContent = `${event.file} / ${event.unit_id}`;
  // index.html ships both buttons empty; their labels only exist here.
  dom.conflictAcceptDisk.textContent = t("ConflictDialog", "Use disk value");
  dom.conflictKeepDraft.textContent = t("ConflictDialog", "Keep editor value");
  if (dom.conflictDialog.hidden) {
    dom.conflictError.hidden = true;
    // The panel, not a choice: the translator is mid-word, and the next Space
    // or Enter must not answer a dialog nobody has read yet.
    showDialog(dom.conflictDialog, dom.conflictPanel);
  }
}

async function resolveConflict(action, keepsTheDraft = false) {
  try {
    await action();
  } catch (err) {
    if (keepsTheDraft) {
      // The draft is in no file yet; closing the dialog would drop it.
      dom.conflictError.textContent = err.message;
      dom.conflictError.hidden = false;
      return;
    }
    showError(err);
  }
  hideDialog(dom.conflictDialog);
  if (activeScreen && activeScreen.refresh) {
    Promise.resolve(activeScreen.refresh()).catch((err) => showError(err));
  }
}

trapFocus(dom.conflictDialog);
dom.conflictAcceptDisk.addEventListener("click", () => resolveConflict(() => bridge.accept_disk()));
dom.conflictKeepDraft.addEventListener("click", () => resolveConflict(() => bridge.keep_draft(), true));

// -- shared context --------------------------------------------------------

const ctx = {
  bridge,
  t,
  appState,
  showError,
  onEvent,
  startJob,
  // The running job's details, as the job bar's panel shows them.
  jobLog: {
    lines: jobLogLines,
    onChange(listener) {
      jobLogListeners.add(listener);
      return () => jobLogListeners.delete(listener);
    },
  },
  cancelJob,
  pauseJob,
  resumeJob,
  navigate,
  openProject,
  openCreateProjectDialog: (onCreated) => openCreateProjectDialog(ctx, onCreated),
  appearance,
  setAppearance,
  locale: () => uiLocale,
  refreshQuality,
  onQuality,
  setLocale,
  conflictPending: () => !dom.conflictDialog.hidden,
};

onEvent((event) => {
  if (event.type === "conflict") {
    showConflict(event);
    return;
  }
  if (event.job === "translate" && typeof appState !== "undefined") {
    appState.translationRun = updateTranslationRun(appState.translationRun, event);
  }
  // A reply still arriving is the dashboard's to show; the bar keeps the last batch.
  if (event.type === "progress" && event.transient) return;
  const owner = activeJobOwner;
  if (owner === null || event.job !== owner.name) return;
  if (["succeeded", "failed", "cancelled"].includes(event.type)) {
    activeJobOwner = null;
    jobPaused = false;
  }
  if (event.type === "progress") {
    pendingProgress.set(progressKey(event), { event, owner });
    if (progressFrameId !== null) return;
    progressFrameId = requestAnimationFrame(() => {
      progressFrameId = null;
      const latest = [...pendingProgress.values()];
      pendingProgress.clear();
      for (const progress of latest) {
        if (activeJobOwner === progress.owner) applyProgress(progress.event);
      }
    });
  } else if (event.type === "paused") {
    jobPaused = true;
    setJobBarState("paused");
    dom.jobStatus.textContent = t("JobBar", "Paused: {name}", { name: event.job });
    announceJobState();
    appendLog("PAUSED");
  } else if (event.type === "resumed") {
    jobPaused = false;
    setJobBarState("running");
    dom.jobStatus.textContent = t("JobBar", "Running: {name}", { name: event.job });
    announceJobState();
    appendLog("RESUMED");
  } else if (event.type === "log") {
    appendLog(event.message ?? "");
  } else if (event.type === "succeeded") {
    if (progressFrameId !== null) cancelAnimationFrame(progressFrameId);
    progressFrameId = null;
    pendingProgress.clear();
    clearProgressDetail();
    setJobBarState("succeeded");
    dom.jobCancelButton.disabled = true;
    dom.jobProgress.max = "1";
    dom.jobProgress.value = "1";
    dom.jobPercentage.textContent = "100%";
    dom.jobStatus.textContent = t("JobBar", "Completed: {name}", { name: event.job });
    announceJobState();
    appendLog(`DONE ${event.job}`);
  } else if (event.type === "failed") {
    if (progressFrameId !== null) cancelAnimationFrame(progressFrameId);
    progressFrameId = null;
    pendingProgress.clear();
    clearProgressDetail();
    setJobBarState("failed");
    dom.jobCancelButton.disabled = true;
    dom.jobStatus.textContent = t("JobBar", "Failed: {message}", { message: event.message ?? "" });
    appendLog(`ERROR ${event.message ?? ""}`);
    showError({ message: event.message ?? `Job failed: ${event.job}`, kind: event.job });
    setJobExpanded(true);
  } else if (event.type === "cancelled") {
    if (progressFrameId !== null) cancelAnimationFrame(progressFrameId);
    progressFrameId = null;
    pendingProgress.clear();
    clearProgressDetail();
    setJobBarState("cancelled");
    dom.jobCancelButton.disabled = true;
    dom.jobStatus.textContent = t("JobBar", "Cancelled");
    announceJobState();
    appendLog("CANCELLED");
  }
});

// -- sidebar ---------------------------------------------------------------

const navButtons = {};
let navToggleButton = null;

// A rail button carries its glyph and its name side by side; the name only
// shows when the rail is expanded, but it stays in the tree so the collapse
// is a width change and not a rebuild.
function setRailContent(button, glyph, label) {
  const mark = document.createElement("span");
  mark.className = "nav-glyph";
  mark.textContent = glyph;
  const text = document.createElement("span");
  text.className = "nav-label";
  text.textContent = label;
  button.replaceChildren(mark, text);
}

// The toggle's glyph and name flip with the state, so it is repainted whenever
// the appearance changes rather than only when the rail is rebuilt.
function updateNavToggle() {
  if (!navToggleButton) return;
  const expanded = appearance.navExpanded;
  const label = t(
    "Navigation",
    expanded ? "Collapse navigation" : "Expand navigation",
  );
  setRailContent(navToggleButton, expanded ? "<|" : ">|", label);
  navToggleButton.title = label;
  navToggleButton.setAttribute("aria-label", label);
  navToggleButton.setAttribute("aria-expanded", String(expanded));
}

function buildSidebar() {
  dom.sidebar.replaceChildren();
  for (const [key, text, glyph] of RAIL) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "nav-item";
    const label = t("Navigation", text);
    setRailContent(button, glyph, label);
    button.title = label;
    button.setAttribute("aria-label", label);
    button.addEventListener("click", () => navigate(key));
    dom.sidebar.append(button);
    navButtons[key] = button;
  }
  const spacer = document.createElement("span");
  spacer.className = "nav-spacer";
  dom.sidebar.append(spacer);
  const divider = document.createElement("span");
  divider.className = "nav-divider";
  divider.setAttribute("aria-hidden", "true");
  dom.sidebar.append(divider);
  navToggleButton = document.createElement("button");
  navToggleButton.type = "button";
  navToggleButton.className = "nav-item nav-utility";
  navToggleButton.addEventListener("click", () => {
    setAppearance({ navExpanded: !appearance.navExpanded });
  });
  dom.sidebar.append(navToggleButton);
  updateNavToggle();
}

function setSidebarSelection(key) {
  for (const [route, button] of Object.entries(navButtons)) {
    button.classList.toggle("selected", route === key);
    if (route === key) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}

// A screen with a title of its own is headed by it; the others get the name
// their rail button carries, for the screen reader's heading list.
function setPageHeading(route) {
  const own = dom.page.querySelector(`[data-route="${route}"] :is(h1, [role="heading"][aria-level="1"])`);
  dom.pageHeading.textContent = navButtons[route]?.getAttribute("aria-label") ?? "";
  dom.pageHeading.hidden = own !== null || !navButtons[route];
}

// -- routing -----------------------------------------------------------

let activeScreen = null;
let launcherInstance = null;
// The title bar's field belongs to no screen, so what it asked for waits here
// until the route that can answer it is on screen.

function disposeLauncher() {
  if (launcherInstance?.unmount) launcherInstance.unmount();
  launcherInstance = null;
  dom.launcherPage.replaceChildren();
}

function navigate(route) {
  if (location.hash.slice(1) === route) {
    renderRoute();
  } else {
    location.hash = route;
  }
}

async function openProject(root) {
  try {
    const summary = await bridge.open_project(root);
    screenHost.clear();
    activeScreen = null;
    appState.project = summary;
    appState.qaReport = null;
    appState.translationRun = null;
    dom.projectTitle.textContent = summary.name;
    navigate("overview");
    refreshReviewedPercentage();
    refreshQuality();
  } catch (err) {
    showError(err);
  }
}

function renderRoute() {
  const raw = (location.hash || "#launcher").slice(1);
  if (raw === "launcher" || appState.project === null) {
    screenHost.hide();
    dom.launcherPage.hidden = false;
    dom.workspace.hidden = true;
    dom.reviewedPercentage.textContent = "";
    disposeLauncher();
    launcherInstance = launcherScreen.mount(dom.launcherPage, ctx);
    activeScreen = launcherInstance;
    return;
  }
  const module = ROUTES[raw];
  if (!module) {
    location.hash = "overview";
    return;
  }
  disposeLauncher();
  dom.launcherPage.hidden = true;
  dom.workspace.hidden = false;
  activeScreen = screenHost.show(raw, module, ctx);
  setSidebarSelection(raw);
  setPageHeading(raw);
  refreshReviewedPercentage();
}

window.addEventListener("hashchange", renderRoute);

dom.projectTitle.addEventListener("click", () => navigate("launcher"));
dom.jobStatusButton.addEventListener("click", () => {
  if (appState.project !== null) navigate("translation-dashboard");
});

async function setLocale(locale) {
  uiLocale = locale;
  await loadCatalog(bridge, uiLocale);
  await storeAppearance();
  localizeShell();
  disposeLauncher();
  screenHost.clear();
  activeScreen = null;
  buildSidebar();
  setJobExpanded(jobExpanded);
  renderRoute();
}

// -- bootstrap ---------------------------------------------------------

function localizeShell() {
  document.documentElement.lang = UI_LANG[uiLocale] ?? uiLocale.replace("_", "-");
  const projects = t("MainWindow", "Projects");
  dom.projectTitle.title = projects;
  dom.jobCancelButton.textContent = t("JobBar", "Cancel");
  dom.jobProgress.setAttribute("aria-label", t("JobBar", "Progress"));
  localizeWindowControls();
  applyAppearance();
}

async function bootstrap() {
  const stored = await bridge.gui_settings();
  Object.assign(appearance, {
    theme: stored.theme ?? APPEARANCE_DEFAULTS.theme,
    accent: stored.accent ?? APPEARANCE_DEFAULTS.accent,
    density: stored.density ?? APPEARANCE_DEFAULTS.density,
    navExpanded: stored.nav_expanded ?? APPEARANCE_DEFAULTS.navExpanded,
    fontFamily: stored.font_family ?? APPEARANCE_DEFAULTS.fontFamily,
    fontSize: stored.font_size ?? APPEARANCE_DEFAULTS.fontSize,
  });
  if (stored.locale) uiLocale = stored.locale;
  await loadCatalog(bridge, uiLocale);
  localizeShell();
  setJobBarState("idle");
  dom.jobStatus.textContent = t("JobBar", "Saved");
  setJobExpanded(false);
  buildSidebar();
  renderRoute();
}

bootstrap().catch((err) => showError(err));
