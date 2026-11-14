// replace_panel.js -- "A becomes B" across every translation, listed before it is written.
//
// The list is the safeguard. A literal replace in a language with no word
// boundaries also finds the lines where A is part of another word, so every
// line is shown as it is and as it would be, and can be left out.

import { el, labelControl, setChildren, uniqueId } from "../dom.js";
import { describeDialog, returnFocus, trapFocus } from "../dialog.js";
import { htmlLang } from "../language_select.js";
import { fromFirstMark, markedPieces } from "../replace_marks.js";
import { createVirtualList } from "../virtual_list.js";

const PREVIEW_DELAY_MS = 250;
// The totals arrive with the first rows; a short list never asks again.
const FIRST_PAGE = 50;
const ROWS_SHOWN = 5;
const JOBS = new Set(["replace", "replace-undo"]);
const JOB_ENDED = new Set(["succeeded", "failed", "cancelled"]);

// Three lines of text per row, so the row follows the interface font size.
function rowHeight() {
  const scale = parseFloat(
    getComputedStyle(document.documentElement).getPropertyValue("--font-scale"),
  );
  return Math.round(62 * (scale || 16 / 13));
}

function markedCell(text, marks, className, lang) {
  return el("span", { class: "replace-cell", lang, title: text },
    fromFirstMark(markedPieces(text, marks)).map((piece) =>
      (piece.marked ? el("mark", { class: className, text: piece.text }) : piece.text)));
}

// Resolves once the dialog is closed. `statusChip(status)` draws a status the
// way the queue does.
export function openReplacePanel(ctx, { find = "", statusChip } = {}) {
  const { t } = ctx;
  const say = (source, vars) => t("ReplacePanel", source, vars);
  const sourceLang = htmlLang(ctx.appState.project?.source_lang);
  const targetLang = htmlLang(ctx.appState.project?.target_lang);
  const height = rowHeight();

  let preview = null; // what the list shows: the totals, the first rows, and what was typed
  let stale = false; // the fields moved on from the preview on screen
  let busy = false;
  let previewTimer = null;
  let requestToken = 0;
  const leftOut = new Set(); // each unticked line, as the JSON of [source_file, id]
  const glossaryOff = new Set(); // source of each unticked glossary entry

  const findInput = el("input", { type: "text", value: find });
  const replaceInput = el("input", { type: "text" });
  const statusText = el("span");
  const undoButton = el("button", { class: "btn btn-sm", type: "button", hidden: true });
  const status = el("p", { class: "replace-status", role: "status", hidden: true }, [statusText, undoButton]);
  const totals = el("p", { class: "replace-totals", role: "status" });
  const notes = el("div", { class: "replace-notes" });
  const head = el("div", { class: "replace-head", hidden: true, "aria-hidden": "true" }, [
    el("span"),
    el("span", { text: say("Source") }),
    el("span", { text: say("Now") }),
    el("span", { text: say("After") }),
    el("span"),
  ]);
  const viewport = el("div", { class: "replace-list", hidden: true });
  const glossaryList = el("div");
  const glossary = el("section", { class: "replace-glossary", hidden: true }, [
    el("h3", { class: "replace-glossary-title", text: say("Glossary") }),
    glossaryList,
  ]);
  const error = el("p", { class: "severity-error", role: "alert" });
  const closeButton = el("button", { class: "btn", type: "button", text: say("Close") });
  const applyButton = el("button", { class: "btn btn-primary", type: "button" });
  const field = (label, input) =>
    el("div", { class: "field-row" }, [labelControl(el("label", { text: label }), input), input]);
  const titleId = uniqueId("replace-title");
  // One fieldset, so a running job disables every control in the dialog at once.
  const body = el("fieldset", { class: "replace-body" }, [
    status,
    el("div", { class: "replace-fields" }, [
      field(say("Find"), findInput),
      field(say("Replace with"), replaceInput),
    ]),
    totals,
    notes,
    head,
    viewport,
    glossary,
    error,
    el("div", { class: "modal-actions" }, [closeButton, applyButton]),
  ]);
  const overlay = el("div", { class: "modal-overlay" }, [
    // Focusable, so a click on plain text inside it keeps the keyboard in the dialog.
    el("div", { class: "modal-panel replace-panel", tabindex: "-1" }, [
      el("h2", { class: "card-title", id: titleId, text: say("Replace in translations") }),
      body,
    ]),
  ]);
  const opener = document.activeElement;

  const list = createVirtualList({
    viewport,
    rowHeight: height,
    loadSlice,
    renderRow,
  });

  async function loadSlice(first, count) {
    const shown = preview;
    if (shown === null) return [];
    if (first + count <= shown.rows.length) return shown.rows.slice(first, first + count);
    try {
      return (await ctx.bridge.replace_preview(shown.find, shown.replace, first, count)).rows;
    } catch (err) {
      error.textContent = err.message;
      return [];
    }
  }

  function renderRow(row) {
    const key = JSON.stringify([row.source_file, row.id]);
    const box = el("input", { type: "checkbox", checked: !leftOut.has(key) });
    const node = el("label", { class: leftOut.has(key) ? "replace-row left-out" : "replace-row" }, [
      box,
      el("span", { class: "replace-cell", lang: sourceLang, title: row.source, text: row.source }),
      markedCell(row.before, row.before_marks, "replace-old", targetLang),
      markedCell(row.after, row.after_marks, "replace-new", targetLang),
      el("span", { class: "replace-row-state" }, [
        statusChip?.(row.status),
        row.breaks_format
          ? el("span", { class: "replace-row-flag severity-warning", text: say("Fails the format check") })
          : null,
      ]),
    ]);
    box.addEventListener("change", () => {
      if (box.checked) leftOut.delete(key);
      else leftOut.add(key);
      node.classList.toggle("left-out", !box.checked);
      renderApply();
    });
    return node;
  }

  function renderNotes() {
    const note = (count, source, extra) =>
      (count > 0 ? el("p", { class: "replace-note" }, [say(source, { count }), extra]) : null);
    setChildren(notes, preview === null ? [] : [
      note(preview.breaks_format, "{count} lines would no longer pass the format check."),
      note(preview.emptied, "{count} lines are nothing but this text and are left as they are."),
      note(
        preview.pictures.length,
        "{count} pictures hold this text. Redo them in the image editor:",
        el("span", { class: "replace-pictures mono", text: preview.pictures.join(", ") }),
      ),
    ]);
  }

  function renderGlossary() {
    const entries = preview?.glossary ?? [];
    glossary.hidden = entries.length === 0;
    setChildren(glossaryList, entries.map((entry) => {
      const box = el("input", { type: "checkbox", checked: !glossaryOff.has(entry.source) });
      box.addEventListener("change", () => {
        if (box.checked) glossaryOff.delete(entry.source);
        else glossaryOff.add(entry.source);
        renderApply();
      });
      return el("label", { class: "replace-glossary-row" }, [
        box,
        el("span", { lang: sourceLang, text: entry.source }),
        el("span", { lang: targetLang }, [
          el("mark", { class: "replace-old", text: entry.before }),
          " → ",
          el("mark", { class: "replace-new", text: entry.after }),
        ]),
        entry.origin === "global"
          ? el("span", { class: "muted", text: say("Shared glossary: the project gets its own entry.") })
          : null,
      ]);
    }));
  }

  function tickedGlossary() {
    return (preview?.glossary ?? []).map((entry) => entry.source).filter((source) => !glossaryOff.has(source));
  }

  function renderApply() {
    const lines = preview === null ? 0 : preview.lines - leftOut.size;
    applyButton.textContent = say("Replace in {lines} lines", { lines });
    applyButton.disabled = preview === null || stale || (lines === 0 && tickedGlossary().length === 0);
  }

  function showPreview(next) {
    preview = next;
    stale = false;
    error.textContent = "";
    const lines = preview?.lines ?? 0;
    totals.textContent = preview === null
      ? ""
      : lines === 0
        ? say("Nothing matches.")
        : say("{lines} lines, {occurrences} occurrences, {files} files", preview);
    renderNotes();
    head.hidden = viewport.hidden = lines === 0;
    viewport.style.height = `${Math.min(lines, ROWS_SHOWN) * height}px`;
    viewport.scrollTop = 0;
    list.setTotal(lines);
    renderGlossary();
    renderApply();
  }

  // Find is taken as typed, spaces and all: it is matched literally.
  async function refreshPreview() {
    clearTimeout(previewTimer);
    const token = ++requestToken;
    const [wanted, replacement] = [findInput.value, replaceInput.value];
    if (!wanted || wanted === replacement) {
      showPreview(null);
      return;
    }
    try {
      const page = await ctx.bridge.replace_preview(wanted, replacement, 0, FIRST_PAGE);
      if (token === requestToken) showPreview({ ...page, find: wanted, replace: replacement });
    } catch (err) {
      if (token !== requestToken) return;
      showPreview(null);
      error.textContent = err.message;
    }
  }

  function onTyped() {
    // What was left out belonged to the preview being replaced.
    leftOut.clear();
    glossaryOff.clear();
    stale = true;
    renderApply();
    clearTimeout(previewTimer);
    previewTimer = setTimeout(refreshPreview, PREVIEW_DELAY_MS);
  }

  // The last thing that happened here, and the way back from the last replace
  // while there is one.
  async function showStatus(text = "", { failed = false, justReplaced = false } = {}) {
    let last = { available: false };
    try {
      last = await ctx.bridge.replace_undo_available();
    } catch (err) {
      error.textContent = err.message;
    }
    statusText.textContent = text;
    statusText.classList.toggle("severity-error", failed);
    undoButton.hidden = !last.available;
    if (last.available) {
      undoButton.textContent = justReplaced
        ? say("Undo")
        : say("Undo the last replace ({find} → {replace}, {lines} lines)", last);
    }
    status.hidden = !text && !last.available;
  }

  function setBusy(flag) {
    busy = flag;
    body.disabled = flag;
  }

  async function start(job, options) {
    setBusy(true);
    if (await ctx.startJob(job, options)) return;
    setBusy(false);
    showStatus(t("ErrorBanner", "Wait for the running task to finish."), { failed: true });
  }

  function apply() {
    if (preview === null) return;
    start("replace", {
      find: preview.find,
      replace: preview.replace,
      fingerprint: preview.fingerprint,
      excluded: [...leftOut].map((key) => JSON.parse(key)),
      glossary: tickedGlossary(),
    });
  }

  const unsubscribe = ctx.onEvent((event) => {
    if (!JOBS.has(event.job) || !JOB_ENDED.has(event.type)) return;
    setBusy(false);
    // The button that started the job was disabled under the keyboard, and may be gone.
    findInput.focus();
    if (event.type !== "succeeded") {
      showStatus(event.message ?? "", { failed: true });
      refreshPreview();
    } else if (event.job === "replace") {
      const { detail } = event;
      const said = [
        say("Replaced {occurrences} occurrences in {lines} lines. Glossary entries changed: {glossary}.", detail),
        detail.breaks_format > 0
          ? say("{count} lines would no longer pass the format check.", { count: detail.breaks_format })
          : null,
      ].filter(Boolean).join(" ");
      findInput.value = replaceInput.value = "";
      leftOut.clear();
      glossaryOff.clear();
      showPreview(null);
      showStatus(said, { justReplaced: true });
    } else {
      showStatus(say("Restored {restored} lines. {skipped} were edited since and left as they are.", event.detail));
      refreshPreview();
    }
  });

  return new Promise((resolve) => {
    function close() {
      // The result of a running job is told here; it ends within a moment.
      if (busy) return;
      clearTimeout(previewTimer);
      requestToken += 1;
      unsubscribe();
      overlay.remove();
      returnFocus(opener);
      resolve();
    }

    describeDialog(overlay, { labelledBy: titleId });
    trapFocus(overlay, { onEscape: close });
    closeButton.addEventListener("click", close);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) close();
    });
    findInput.addEventListener("input", onTyped);
    replaceInput.addEventListener("input", onTyped);
    applyButton.addEventListener("click", apply);
    undoButton.addEventListener("click", () => start("replace-undo"));
    document.body.append(overlay);
    (find ? replaceInput : findInput).focus();
    renderApply();
    showStatus();
    refreshPreview();
  });
}
