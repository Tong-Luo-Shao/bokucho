// overview.js -- where the project stands: headline counts, how far each
// source file has got, and the one thing worth doing next.
//
// Status counts arrive in one bridge call so large catalogs are scanned once.

import { el, setChildren } from "../dom.js";

// A LiveMaker release refuses values the game would no longer match; every
// other finding is listed and the translator decides.
const RELEASE_BLOCKING_CHECKS = new Set(["values"]);

const ROUTE_FOR_ACTION = {
  extract: "strings",
  translate: "strings",
  review: "strings",
  qa: "qa",
  release: "release",
};
const SCREEN_NAMES = { strings: "Strings", qa: "Quality", release: "Release" };

function kpi(label, value, note) {
  return el("div", { class: "overview-kpi" }, [
    el("div", { class: "overview-kpi-label", text: label }),
    el("div", { class: "overview-kpi-value", text: value }),
    el("div", { class: "overview-kpi-note", text: note }),
  ]);
}

function panel(title, aside = null) {
  const head = el("div", { class: "overview-panel-head" }, [
    el("span", { class: "overview-panel-title", text: title }),
    el("span", { class: "spacer" }),
    aside ? el("span", { class: "overview-panel-aside", text: aside }) : null,
  ]);
  const body = el("div", { class: "overview-panel-body" });
  return { element: el("section", { class: "overview-panel" }, [head, body]), body, head };
}

// A file's bar is three segments in one track: settled, drafted, and the rest.
export function openFileProgress(ctx, sourceFile) {
  ctx.appState.pendingStringSearch = { query: sourceFile, scope: "id" };
  ctx.navigate("strings");
}

function fileRow(name, total, counts, t, open) {
  const reviewed = counts.reviewed + counts.ignored;
  const drafted = counts.mt;
  const pct = total > 0 ? Math.round((reviewed / total) * 100) : 0;
  const width = (n) => `${total > 0 ? (n / total) * 100 : 0}%`;
  return el("button", { class: "overview-file", type: "button", onclick: open }, [
    el("div", { class: "overview-file-main" }, [
      el("div", { class: "overview-file-line" }, [
        el("span", { class: "overview-file-name", text: name, title: name }),
        el("span", {
          class: "overview-file-units",
          text: t("Overview", "{count} units", { count: total }),
        }),
      ]),
      el("div", { class: "overview-file-track" }, [
        el("span", { class: "overview-file-seg is-reviewed", style: `width:${width(reviewed)}` }),
        el("span", { class: "overview-file-seg is-mt", style: `width:${width(drafted)}` }),
      ]),
    ]),
    el("span", { class: "overview-file-pct", text: `${pct}%` }),
  ]);
}

export function mount(container, ctx) {
  const { bridge, t } = ctx;
  const title = el("span", { class: "overview-title" });
  const meta = el("span", { class: "overview-meta" });
  const kpis = el("div", { class: "overview-kpis" });
  const filesPanel = panel(t("Overview", "File progress"));
  const nextPanel = panel(t("Overview", "Next steps"));
  const signalsPanel = panel(t("Overview", "Project signals"));
  const empty = el("p", {
    class: "overview-empty",
    text: t("Overview", "No text has been extracted yet. Configure the game path to begin."),
  });
  const grid = el("div", { class: "overview-grid" }, [
    filesPanel.element,
    el("div", { class: "overview-column" }, [nextPanel.element, signalsPanel.element]),
  ]);
  container.replaceChildren(el("div", { class: "overview-screen" }, [
    el("div", { class: "overview-head" }, [title, meta]),
    el("div", { class: "overview-body" }, [kpis, grid, empty]),
  ]));

  async function refresh() {
    const project = ctx.appState.project;
    if (project === null) return;
    const summary = await bridge.unit_counts();
    const qaReport = ctx.appState.qaReport;
    title.textContent = project.name ?? "";
    meta.textContent = t("Overview", "{source} -> {target} / {engine}", {
      source: project.source_lang,
      target: project.target_lang,
      engine: project.engine,
    });
    setChildren(kpis, kpiCards(summary, qaReport, t));
    setChildren(filesPanel.body, (summary.files ?? []).map((file) =>
      fileRow(file.source_file, file.total, file.status_counts, t, () =>
        openFileProgress(ctx, file.source_file),
      ),
    ));
    setChildren(nextPanel.body, nextSteps(summary, qaReport, ctx));
    setChildren(signalsPanel.body, projectSignals(summary, qaReport, t));
    kpis.hidden = summary.total === 0;
    grid.hidden = summary.total === 0;
    empty.hidden = summary.total !== 0;
  }

  refresh().catch((err) => ctx.showError(err));
  const unsubscribe = ctx.onQuality(() => refresh().catch((err) => ctx.showError(err)));

  return { refresh, unmount: unsubscribe };
}

function qaDefects(qaReport) {
  return qaReport?.issues.filter((issue) => issue.severity !== "warning") ?? [];
}

function kpiCards(summary, qaReport, t) {
  const counts = summary.status_counts;
  const warnings = qaReport?.issues.filter((issue) => issue.severity === "warning") ?? [];
  return [
    kpi(
      t("Overview", "Text units"),
      String(summary.total),
      t("Overview", "Image units: {count}", { count: summary.image_units }),
    ),
    kpi(
      t("Overview", "Untranslated"),
      String(counts.untranslated),
      t("Overview", "Translated: {count}", { count: summary.total - counts.untranslated }),
    ),
    kpi(
      t("Overview", "Review pending"),
      String(counts.mt),
      t("Overview", "Reviewed: {count}", { count: counts.reviewed }),
    ),
    kpi(
      t("Overview", "QA defects"),
      qaReport === null ? "--" : String(qaDefects(qaReport).length),
      qaReport === null
        ? t("Overview", "QA not run")
        : t("Overview", "{count} warnings", { count: warnings.length }),
    ),
  ];
}

function nextActionKey(summary, qaReport) {
  const counts = summary.status_counts;
  if (summary.total === 0) return "extract";
  if (counts.untranslated) return "translate";
  if (counts.mt) return "review";
  if (qaReport === null) return "qa";
  return "release";
}

function nextSteps(summary, qaReport, ctx) {
  const { t } = ctx;
  const actionKey = nextActionKey(summary, qaReport);
  const actionLabels = {
    extract: t("Overview", "Configure extraction"),
    translate: t("Overview", "Open translation queue"),
    review: t("Overview", "Review machine drafts"),
    qa: t("Overview", "Run quality checks"),
    release: t("Overview", "Prepare release"),
  };
  const route = ROUTE_FOR_ACTION[actionKey];
  return [
    nextRow(actionLabels[actionKey], route, () => ctx.navigate(route), t),
    nextRow(t("Overview", "Translation dashboard"), null, () =>
      ctx.navigate("translation-dashboard"), t),
  ];
}

function nextRow(label, route, action, t) {
  const button = el("button", { class: "overview-next", type: "button" }, [
    el("span", { class: "overview-next-label", text: label }),
    route === null
      ? null
      : el("span", {
          class: "overview-next-where",
          text: `${t("Navigation", SCREEN_NAMES[route])} →`,
        }),
  ]);
  button.addEventListener("click", action);
  return button;
}

function releaseReadiness(qaReport, t) {
  if (qaReport === null) return t("Overview", "QA not run");
  const blockers = qaDefects(qaReport).filter((issue) => RELEASE_BLOCKING_CHECKS.has(issue.check));
  if (blockers.length) {
    return t("Overview", "Release blocked by {count} matched value(s)", { count: blockers.length });
  }
  const toLookAt = qaReport.issues.length - blockers.length;
  if (toLookAt) {
    return t("Overview", "Release ready with {count} finding(s) to look at", { count: toLookAt });
  }
  return t("Overview", "Release ready");
}

function projectSignals(summary, qaReport, t) {
  const untranslated = summary.status_counts.untranslated;
  return [
    el("p", { class: "overview-signal", text: releaseReadiness(qaReport, t) }),
    untranslated
      ? el("p", {
          class: "overview-signal",
          text: t("Overview", "{count} unit(s) are untranslated and ship as original text.", {
            count: untranslated,
          }),
        })
      : null,
    el("p", {
      class: "overview-signal muted",
      text: t("Overview", "Image units: {count}", { count: summary.image_units }),
    }),
  ];
}
