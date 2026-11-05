// launcher.js -- recent-project list, an open-by-path form, and the
// create-project dialog entry point.

import { el, setChildren, uniqueId } from "../dom.js";
import { createDirectoryPicker } from "../directory_picker.js";

export function mount(container, ctx) {
  const { bridge, t } = ctx;
  const error = el("p", { class: "severity-error" });
  const list = el("div", { id: "recentProjectsList", class: "recent-project-list" });
  container.replaceChildren(el("div", { class: "launcher-screen" }, [
    launcherHeader(t),
    launcherActions(ctx, error),
    error,
    el("section", { class: "recent-projects" }, [
      el("h2", { class: "section-heading", text: t("Launcher", "Recent projects") }),
      list,
    ]),
  ]));

  function renderRecords(records) {
    setChildren(list, records.length
      ? records.map((record) => recentProjectRow(record, ctx, renderRecords))
      : el("p", { class: "muted", text: t("Launcher", "No recent projects yet.") }));
  }

  async function refresh() {
    renderRecords(await bridge.recent_projects());
  }

  refresh().catch((err) => ctx.showError(err));

  return { refresh, unmount() {} };
}

function launcherHeader(t) {
  return el("div", { class: "page-header" }, [
    el("div", { class: "heading" }, [
      el("h1", { class: "page-title", text: t("Launcher", "Projects") }),
      el("p", {
        class: "page-description",
        text: t("Launcher", "Create or resume a translation workspace."),
      }),
    ]),
  ]);
}

function launcherActions(ctx, error) {
  const { t } = ctx;
  const projectPicker = createDirectoryPicker(ctx, {
    id: "openProjectPath",
    label: t("Launcher", "Project folder path"),
    placeholder: t("Launcher", "Project folder path"),
    browseText: t("Launcher", "Browse"),
    onError: (err) => {
      error.textContent = err.message;
    },
  });
  const openButton = el("button", { class: "btn btn-primary", type: "button", text: t("Launcher", "Open folder") });
  openButton.addEventListener("click", () => {
    const path = projectPicker.input.value.trim();
    if (path) ctx.openProject(path);
  });
  const createButton = el("button", { class: "btn btn-primary", type: "button", text: t("CreateProject", "Create project") });
  createButton.addEventListener("click", () => {
    ctx.openCreateProjectDialog((created) => {
      ctx.openProject(created.root);
    });
  });
  return el("div", { class: "button-bar launcher-actions" }, [
    createButton,
    el("div", { class: "project-open-row" }, [projectPicker.element, openButton]),
  ]);
}

// The card opens the project through its open button, which a stretched
// pseudo-element widens to the whole card; the forget button is its sibling
// rather than a control nested in another.
function recentProjectRow(record, ctx, renderRecords) {
  const { t } = ctx;
  const unavailable = record.available === false;
  const path = el("div", { class: "recent-project-path mono", id: uniqueId("recent-path"), text: record.path });
  const unavailableChip = unavailable
    ? el("span", { class: "chip chip-error", id: uniqueId("recent-unavailable"), text: t("Launcher", "Unavailable") })
    : null;
  const open = el("button", {
    class: "recent-project-open",
    type: "button",
    "aria-describedby": [path, unavailableChip].filter(Boolean).map((node) => node.id).join(" "),
  }, [
    el("strong", { class: "recent-project-name", text: displayName(record) }),
  ]);
  open.addEventListener("click", () => {
    if (unavailable) {
      ctx.showError({ message: `Project is unavailable: ${record.path}` });
      return;
    }
    ctx.openProject(record.path);
  });
  return el("div", { class: "recent-project" }, [
    el("div", { class: "recent-project-copy" }, [open, path, recentProjectFacts(record, t)]),
    unavailableChip,
    forgetButton(record, ctx, renderRecords),
    el("span", { class: "recent-project-go", "aria-hidden": "true", text: "→" }),
  ]);
}

function recentProjectFacts(record, t) {
  const languages = record.source_lang && record.target_lang
    ? `${record.source_lang} → ${record.target_lang}`
    : null;
  return el("div", { class: "recent-project-facts" }, [
    record.engine ? el("span", { class: "chip", text: record.engine }) : null,
    languages ? el("span", { class: "chip chip-quiet", text: languages }) : null,
    el("span", { class: "recent-project-when", text: whenLastOpened(record.last_opened, t) }),
  ]);
}

// Forgetting is about the list. The folder, its strings and its translations
// are not this button's business.
function forgetButton(record, ctx, renderRecords) {
  const forget = el("button", {
    class: "recent-project-forget",
    type: "button",
    title: ctx.t("Launcher", "Remove from this list"),
    "aria-label": ctx.t("Launcher", "Remove from this list"),
    text: "×",
  });
  forget.addEventListener("click", async () => {
    try {
      renderRecords(await ctx.bridge.forget_project(record.path));
    } catch (err) {
      ctx.showError(err);
    }
  });
  return forget;
}

// "2026-08-04T08:27:21.973292Z" is a timestamp, not something to read. What
// a person wants from a recent-project list is how long ago they were last
// in it.
function whenLastOpened(stamp, t) {
  const opened = stamp ? new Date(stamp) : null;
  if (!opened || Number.isNaN(opened.getTime())) return "";
  const minutes = Math.round((Date.now() - opened.getTime()) / 60000);
  if (minutes < 1) return t("Launcher", "Just now");
  if (minutes < 60) return t("Launcher", "{n} min ago", { n: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 24) return t("Launcher", "{n} h ago", { n: hours });
  const days = Math.round(hours / 24);
  if (days < 30) return t("Launcher", "{n} d ago", { n: days });
  return opened.toLocaleDateString();
}

// The folder is the project's name when it has none of its own, and the path
// is then said once rather than twice.
function displayName(record) {
  if (record.name) return record.name;
  const parts = String(record.path).replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || record.path;
}
