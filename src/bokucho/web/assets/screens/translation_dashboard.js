// translation_dashboard.js -- live performance and provider usage for one run.

import { confirmDialog } from "../confirm.js";
import { el, setChildren } from "../dom.js";
import { createLanguageSelect, selectLanguage } from "../language_select.js";
import { formatDuration, tokenUsage, translationTiming } from "../translation_run.js";

const INTEGER = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const DECIMAL = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

export async function startTranslation(ctx, languages, options) {
  const saved = await ctx.bridge.save_project_settings(
    null, languages.source_lang, languages.target_lang,
  );
  if (ctx.appState.project) {
    ctx.appState.project.source_lang = saved.source_lang ?? languages.source_lang;
    ctx.appState.project.target_lang = saved.target_lang ?? languages.target_lang;
  }
  return ctx.startJob("translate", options);
}

function primaryMetric(label, value, kind = "") {
  return el("div", { class: `translation-dashboard-metric ${kind}`.trim() }, [
    el("span", { class: "translation-dashboard-metric-label", text: label }),
    el("strong", { class: "translation-dashboard-metric-value", text: value }),
  ]);
}

function detailMetric(label, value) {
  return el("div", { class: "translation-dashboard-stat" }, [
    el("dt", { text: label }),
    el("dd", { text: value }),
  ]);
}

function statusLabel(t, status) {
  if (status === "running") return t("TranslationDashboard", "Running");
  if (status === "succeeded") return t("TranslationDashboard", "Completed");
  if (status === "failed") return t("TranslationDashboard", "Failed");
  if (status === "cancelled") return t("TranslationDashboard", "Cancelled");
  if (status === "paused") return t("TranslationDashboard", "Paused");
  return t("TranslationDashboard", "Not started");
}

function isLive(status) {
  return status === "running" || status === "paused";
}

function dashboardHead(t, status, activeInterface) {
  const interfaceLabel = activeInterface
    ? `${activeInterface.name} / ${activeInterface.provider} / ${activeInterface.model}`
    : t("TranslationDashboard", "No translation interface selected");
  return el("div", { class: "translation-dashboard-head" }, [
    el("div", {}, [
      el("h2", {
        class: "translation-dashboard-title",
        text: t("TranslationDashboard", "Current run"),
      }),
      el("p", { class: "translation-dashboard-model", text: interfaceLabel }),
    ]),
    el("span", { class: "translation-dashboard-status" }, [
      el("span", { class: "translation-dashboard-status-dot" }),
      statusLabel(t, status),
    ]),
  ]);
}

function idlePanel(ctx, activeInterface) {
  const { t } = ctx;
  return el("div", { class: "translation-dashboard-idle" }, [
    el("p", {
      text: t(
        "TranslationDashboard",
        "Once a run starts, progress, elapsed time and ETA, token usage, throughput and failed units update here live.",
      ),
    }),
    el("p", {
      class: "muted",
      text: t(
        "TranslationDashboard",
        "Start translates the lines that are still untranslated. Reviewed lines are never changed.",
      ),
    }),
    activeInterface === null
      ? el("button", {
          class: "btn",
          type: "button",
          text: t("TranslationDashboard", "Choose translation interface"),
          onclick: () => ctx.navigate("model"),
        })
      : null,
  ]);
}

function progressCard(t, run, status) {
  const processed = run.processed ?? 0;
  const total = run.total ?? 0;
  const percentage = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
  return el("div", { class: "translation-dashboard-metric translation-dashboard-progress-card" }, [
    el("span", {
      class: "translation-dashboard-metric-label",
      text: t("TranslationDashboard", "Progress"),
    }),
    el("div", {
      class: "translation-dashboard-ring",
      style: `--progress-angle: ${percentage * 3.6}deg`,
      role: "progressbar",
      "aria-label": t("TranslationDashboard", "Completed units"),
      "aria-valuemin": "0",
      "aria-valuemax": String(Math.max(total, 1)),
      "aria-valuenow": total > 0 ? String(Math.min(processed, total)) : undefined,
    }, [
      el("strong", {
        class: "translation-dashboard-ring-value",
        text: total > 0 ? `${percentage}%` : "--",
      }),
    ]),
    el("span", { class: "translation-dashboard-metric-note", text: statusLabel(t, status) }),
  ]);
}

function countsCard(t, run) {
  const processed = run.processed ?? 0;
  const total = run.total ?? 0;
  const count = (label, value) => el("div", {}, [
    el("span", { class: "translation-dashboard-count-label", text: label }),
    el("strong", { class: "translation-dashboard-metric-value", text: total > 0 ? INTEGER.format(value) : "--" }),
  ]);
  return el("div", { class: "translation-dashboard-metric translation-dashboard-counts" }, [
    el("span", {
      class: "translation-dashboard-metric-label",
      text: t("TranslationDashboard", "Unit counts"),
    }),
    el("div", { class: "translation-dashboard-count-values" }, [
      count(t("TranslationDashboard", "Completed units"), processed),
      count(t("TranslationDashboard", "Remaining units"), Math.max(0, total - processed)),
    ]),
    el("span", {
      class: "translation-dashboard-metric-note mono",
      text: total > 0
        ? t("TranslationDashboard", "{current} / {total}", {
            current: INTEGER.format(processed),
            total: INTEGER.format(total),
          })
        : "--",
    }),
  ]);
}

function primaryRow(t, run, status, timing) {
  return el("div", { class: "translation-dashboard-primary" }, [
    progressCard(t, run, status),
    countsCard(t, run),
    primaryMetric(
      t("TranslationDashboard", "Elapsed"),
      formatDuration(timing.elapsedSeconds),
      "translation-dashboard-time",
    ),
    primaryMetric("ETA", formatDuration(timing.etaSeconds), "translation-dashboard-time"),
  ]);
}

function secondaryRow(t, run, timing) {
  const usage = tokenUsage(run);
  const tokenValue = usage.estimated && usage.tokens === 0
    ? "--"
    : `${usage.estimated ? "~" : ""}${INTEGER.format(usage.tokens)}`;
  const throughput = timing.unitsPerMinute === null
    ? "--"
    : t("TranslationDashboard", "{count}/min", {
        count: DECIMAL.format(timing.unitsPerMinute),
      });
  return el("div", { class: "translation-dashboard-secondary" }, [
    primaryMetric(t("TranslationDashboard", "Token usage"), tokenValue),
    primaryMetric(t("TranslationDashboard", "Throughput"), throughput),
    primaryMetric(t("TranslationDashboard", "Parallel lanes"), INTEGER.format(run.lanes)),
    primaryMetric(
      t("TranslationDashboard", "Failed units"),
      INTEGER.format(run.failed),
      run.failed > 0 ? "has-errors" : "",
    ),
  ]);
}

function runStats(t, run) {
  const reported = (count) => (run.token_usage_reported ? INTEGER.format(count) : "--");
  return el("dl", { class: "translation-dashboard-stats" }, [
    detailMetric(t("TranslationDashboard", "Translated"), INTEGER.format(run.translated)),
    detailMetric(t("TranslationDashboard", "Batches"), INTEGER.format(run.batches)),
    detailMetric(t("TranslationDashboard", "Input tokens"), reported(run.input_tokens)),
    detailMetric(t("TranslationDashboard", "Output tokens"), reported(run.output_tokens)),
    detailMetric(t("TranslationDashboard", "Cached input"), reported(run.cached_input_tokens)),
    detailMetric(t("TranslationDashboard", "Requests"), INTEGER.format(run.requests)),
  ]);
}

function usageNote(t, run) {
  if (run.token_usage_reported) return t("TranslationDashboard", "Provider-reported usage");
  if (run.estimated_input_tokens) {
    return t(
      "TranslationDashboard",
      "Input tokens are estimated because this endpoint reports no usage.",
    );
  }
  return null;
}

function runFoot(t, run) {
  const note = usageNote(t, run);
  const foot = el("div", { class: "translation-dashboard-foot" }, [
    note ? el("span", { class: "muted", text: note }) : null,
    run.status === "running" && run.streamed_chars > 0
      ? el("span", {
          class: "mono translation-dashboard-receiving",
          role: "status",
          text: t("TranslationDashboard", "Receiving reply: {count} characters", {
            count: INTEGER.format(run.streamed_chars),
          }),
        })
      : null,
    run.detail
      ? el("span", {
          class: "mono translation-dashboard-file",
          text: t("TranslationDashboard", "Current file: {file}", { file: run.detail }),
        })
      : null,
  ]);
  return foot.childNodes.length ? foot : null;
}

export function mount(container, ctx) {
  const screen = new TranslationDashboard(ctx);
  container.replaceChildren(screen.element);
  const unsubscribe = ctx.onEvent((event) => screen.onEvent(event));
  const timer = window.setInterval(() => {
    if (ctx.appState.translationRun?.status === "running") screen.render();
  }, 1_000);
  const refresh = () => screen.refresh();

  screen.render();
  refresh().catch((error) => ctx.showError(error));

  return {
    refresh,
    unmount() {
      window.clearInterval(timer);
      screen.dispose();
      unsubscribe();
    },
  };
}

class TranslationDashboard {
  constructor(ctx) {
    const { t } = ctx;
    this.ctx = ctx;
    this.activeInterface = null;
    this.languagesReady = false;
    this.starting = false;
    // Restarting a live run waits for it to end; the new run starts on the
    // event that says so, because the backend runs one job at a time.
    this.restartPending = false;
    this.sourceLang = createLanguageSelect(ctx, {
      id: "run-source-language",
      label: t("ProjectSettings", "Source language"),
      detected: true,
      value: "und",
    });
    this.targetLang = createLanguageSelect(ctx, {
      id: "run-target-language",
      label: t("ProjectSettings", "Target language"),
      value: ctx.appState.project?.target_lang ?? "zh-tw",
    });
    this.closeRunMenu = () => {
      this.runMenu.hidden = true;
      this.runMenuButton.setAttribute("aria-expanded", "false");
    };
    this.dashboard = el("section", { class: "translation-dashboard" });
    this.element = el("div", { class: "translation-dashboard-screen" }, [this.header(), this.dashboard]);
  }

  header() {
    const { t } = this.ctx;
    return el("div", { class: "page-header" }, [
      el("div", { class: "heading" }, [
        el("h1", {
          class: "page-title",
          text: t("TranslationDashboard", "Translation dashboard"),
        }),
        el("p", {
          class: "page-description",
          text: t(
            "TranslationDashboard",
            "Live performance and provider usage for the current translation run.",
          ),
        }),
      ]),
      el("div", { class: "translation-dashboard-actions" }, [
        el("div", { class: "translation-language-controls" }, [
          this.sourceLang,
          el("span", { class: "muted", "aria-hidden": "true", text: "→" }),
          this.targetLang,
        ]),
        this.runSplit(),
        el("button", {
          class: "btn",
          type: "button",
          text: t("TranslationDashboard", "Open translation queue"),
          onclick: () => this.ctx.navigate("strings"),
        }),
      ]),
    ]);
  }

  // One split button: the half that is clicked over and over follows the run
  // (pause it, resume it, or start one), and starting again from zero sits
  // behind the caret, out of reach of a stray click.
  runSplit() {
    const { t } = this.ctx;
    const showError = (error) => this.ctx.showError(error);
    this.runButton = el("button", {
      class: "btn btn-primary split-main",
      type: "button",
      onclick: () => this.runAction().catch(showError),
    });
    this.runMenuButton = el("button", {
      class: "btn btn-primary split-toggle",
      type: "button",
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      "aria-label": t("TranslationDashboard", "More run actions"),
      text: "⌄",
    });
    this.runMenu = el("div", { class: "split-menu", role: "menu", hidden: true }, [
      el("button", {
        class: "split-menu-item",
        type: "button",
        role: "menuitem",
        text: t("TranslationDashboard", "Restart"),
        onclick: () => {
          this.closeRunMenu();
          this.restart().catch(showError);
        },
      }),
    ]);
    this.runMenuButton.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = this.runMenu.hidden;
      this.runMenu.hidden = !open;
      this.runMenuButton.setAttribute("aria-expanded", String(open));
    });
    this.runMenu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this.closeRunMenu();
    });
    document.addEventListener("click", this.closeRunMenu);
    return el("div", { class: "split-button" }, [this.runButton, this.runMenuButton, this.runMenu]);
  }

  dispose() {
    document.removeEventListener("click", this.closeRunMenu);
  }

  async beginTranslation(options) {
    if (this.starting) return;
    this.starting = true;
    this.render();
    try {
      return await startTranslation(this.ctx, {
        source_lang: this.sourceLang.value,
        target_lang: this.targetLang.value,
      }, options);
    } finally {
      this.starting = false;
      this.render();
    }
  }

  async runAction() {
    const status = this.ctx.appState.translationRun?.status;
    if (status === "running") {
      await this.ctx.pauseJob();
    } else if (status === "paused") {
      await this.ctx.resumeJob();
    } else {
      // Whatever an earlier run finished is saved; a new one picks up the
      // lines still untranslated.
      await this.beginTranslation();
    }
  }

  async restart() {
    const { t } = this.ctx;
    const confirmed = await confirmDialog(this.ctx, {
      message: t(
        "TranslationDashboard",
        "Restart starts from zero: every machine draft and untranslated line is translated again, and each existing draft is replaced by its new translation. Reviewed lines stay as they are.",
      ),
      confirmText: t("TranslationDashboard", "Continue"),
    });
    if (!confirmed) return;
    if (isLive(this.ctx.appState.translationRun?.status)) {
      this.restartPending = true;
      this.updateControls();
      await this.ctx.cancelJob();
    } else {
      await this.beginTranslation({ redo: true });
    }
  }

  updateControls() {
    const { t } = this.ctx;
    const status = this.ctx.appState.translationRun?.status ?? "idle";
    const busy = this.restartPending || this.starting;
    const canStart = this.activeInterface !== null && this.languagesReady && !busy;
    this.sourceLang.disabled = this.targetLang.disabled = !this.languagesReady || isLive(status) || busy;
    if (status === "running") {
      this.runButton.textContent = t("TranslationDashboard", "Pause");
      this.runButton.disabled = busy;
    } else if (status === "paused") {
      this.runButton.textContent = t("TranslationDashboard", "Resume");
      this.runButton.disabled = busy;
    } else {
      this.runButton.textContent = t("TranslationDashboard", "Start");
      this.runButton.disabled = !canStart;
    }
    this.runMenuButton.disabled = !canStart;
    if (!canStart) this.closeRunMenu();
  }

  render() {
    const { t } = this.ctx;
    this.updateControls();
    const run = this.ctx.appState.translationRun;
    const status = run?.status ?? "idle";
    this.dashboard.dataset.status = status;
    const head = dashboardHead(t, status, this.activeInterface);
    if (!run) {
      setChildren(this.dashboard, [head, idlePanel(this.ctx, this.activeInterface)]);
      return;
    }
    const timing = translationTiming(run);
    setChildren(this.dashboard, [
      head,
      primaryRow(t, run, status, timing),
      secondaryRow(t, run, timing),
      runStats(t, run),
      run.status === "failed" && run.error
        ? el("p", {
            class: "translation-dashboard-error severity-error mono",
            role: "alert",
            text: t("TranslationDashboard", "Stopped: {message}", { message: run.error }),
          })
        : null,
      runFoot(t, run),
    ]);
  }

  async refresh() {
    const { bridge, appState } = this.ctx;
    const [catalog, settings] = await Promise.all([
      bridge.provider_interfaces(), bridge.get_settings(),
    ]);
    this.activeInterface = catalog.interfaces.find(
      (item) => item.id === catalog.active_interface_id,
    ) ?? null;
    if (!this.languagesReady) {
      selectLanguage(this.targetLang, settings.target_lang ?? appState.project?.target_lang ?? "zh-tw");
      if (isLive(appState.translationRun?.status)) {
        selectLanguage(this.sourceLang, settings.source_lang ?? "und");
      }
      this.languagesReady = true;
    }
    this.render();
  }

  onEvent(event) {
    if (event.job !== "translate") return;
    if (this.restartPending && ["succeeded", "failed", "cancelled"].includes(event.type)) {
      this.restartPending = false;
      // After the app's own handler has released the job slot.
      window.setTimeout(() => {
        this.beginTranslation({ redo: true }).catch((error) => this.ctx.showError(error));
      }, 0);
    }
    this.render();
  }
}
