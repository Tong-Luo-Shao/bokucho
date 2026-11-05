// release.js -- the release wizard. Every engine ships one patcher.exe:
// LiveMaker and RPG Maker XP, VX and VX Ace carry a recipe the player's patcher
// compiles, engines with a native translation loader an overlay it installs.

import { el } from "../dom.js";
import { loadFace } from "../font_faces.js";

const WIZARD_STEPS = [
  { key: "check", label: "Check" },
  { key: "options", label: "Options" },
  { key: "build", label: "Build" },
  { key: "done", label: "Done" },
];

// The faces the game text font field offers: a placeholder that cannot be
// chosen, the chosen face even before the list arrives, then the list.
export function gameFontOptions(chosen, installed, t) {
  const options = [{ value: "", label: t("ReleaseScreen", "Choose a font"), disabled: true }];
  const listed = installed ?? [];
  if (chosen && !listed.some((entry) => entry.path === chosen.path)) {
    options.push({ value: chosen.path, label: chosen.name ?? chosen.path });
  }
  for (const entry of listed) options.push({ value: entry.path, label: entry.name });
  return options;
}

// A LiveMaker release redraws the game's own font, so it waits for a face to
// draw from; other engines build without one.
export function canBuild(engine, gameFont) {
  return engine !== "livemaker" || Boolean(gameFont?.font);
}

// The DLL the patch carries: the translator's own, else the one Bokucho carries.
export function uifLabel(uif, t) {
  if (uif.path) return uif.path;
  return uif.bundled ? t("ReleaseScreen", "Bokucho's own") : t("ReleaseScreen", "Not chosen");
}

// The queue's own counts, so this step and the overview report one project.
export function checkFigures(unitCounts) {
  if (!unitCounts) return null;
  const { untranslated, mt, reviewed } = unitCounts.status_counts;
  return { total: unitCounts.total, untranslated, pending: mt, reviewed };
}

const NEXT_LABELS = {
  check: "Next",
  options: "Build now",
  build: "Building...",
  done: "Build again",
};

function summaryRow(label, value) {
  return el("div", { class: "metric-card" }, [
    el("div", { class: "metric-label", text: label }),
    el("div", { class: "metric-value", text: value }),
  ]);
}

function pathField(t, message, path, muted = false) {
  const label = t("ReleaseScreen", message, { path: "" }).trim();
  const field = el("input", {
    class: "mono",
    type: "text",
    value: path,
    readonly: true,
    "aria-label": label,
  });
  field.addEventListener("focus", () => field.select());
  return el("label", { class: `release-path${muted ? " muted" : ""}` }, [
    el("span", { text: label }),
    field,
  ]);
}

export function mount(container, ctx) {
  const wizard = new ReleaseWizard(ctx);
  container.replaceChildren(wizard.element);
  const unsubscribe = ctx.onEvent((event) => wizard.onEvent(event));
  const unsubscribeLog = ctx.jobLog.onChange(() => wizard.showLog());

  wizard.render();
  wizard.loadUnitCounts().catch((err) => ctx.showError(err));
  wizard.loadGameFonts().catch((err) => ctx.showError(err));

  async function refresh() {
    wizard.render();
    await wizard.loadUnitCounts();
  }

  return {
    refresh,
    unmount() {
      unsubscribe();
      unsubscribeLog();
    },
  };
}

class ReleaseWizard {
  constructor(ctx) {
    const { t } = ctx;
    this.ctx = ctx;
    this.stepIndex = 0;
    this.result = null;
    this.unitCounts = null;
    this.gameFont = { font: null, sample: [] };
    this.gameFonts = null;
    // The chosen face as the webview loaded it from its file; status is
    // "loading", "ready" or "failed".
    this.preview = { path: null, family: null, status: "loading" };
    this.uif = { path: null, bundled: false };
    this.advancedOpen = false;
    // The build step's log, while it is on screen.
    this.terminal = null;
    this.rail = el("nav", { class: "release-rail", "aria-label": t("ReleaseScreen", "Steps") });
    this.stepTitle = el("div", { class: "release-head" });
    this.body = el("div", { class: "release-body" });
    this.status = el("span", { class: "release-status" });
    this.backButton = el("button", {
      class: "btn btn-sm", type: "button", text: t("ReleaseScreen", "Back"), onClick: () => this.back(),
    });
    this.goButton = el("button", {
      class: "btn btn-sm", type: "button", text: t("ReleaseScreen", "Go"), hidden: true,
      onClick: () => this.openFolder(),
    });
    this.nextButton = el("button", { class: "btn btn-sm btn-primary", type: "button", onClick: () => this.next() });
    this.element = el("div", { class: "release-screen" }, [
      this.rail,
      el("div", { class: "release-main" }, [
        this.stepTitle,
        this.body,
        el("footer", { class: "release-foot" }, [
          this.status,
          el("span", { class: "spacer" }),
          this.backButton,
          this.goButton,
          this.nextButton,
        ]),
      ]),
    ]);
  }

  engine() {
    return this.ctx.appState.project?.engine ?? null;
  }

  async loadUnitCounts() {
    this.unitCounts = await this.ctx.bridge.unit_counts();
    this.render();
  }

  async loadGameFonts() {
    const { bridge } = this.ctx;
    if (this.engine() !== "livemaker") return;
    this.gameFont = await bridge.game_font();
    this.uif = await bridge.uif_dll();
    this.render();
    // Reading every installed face takes seconds; the field works meanwhile.
    this.gameFonts = (await bridge.game_fonts()).fonts;
    this.render();
  }

  // Each change re-renders the step, whether or not it went through.
  async settle(change) {
    try {
      await change();
    } catch (err) {
      this.ctx.showError(err);
    } finally {
      this.render();
    }
  }

  gameFontField() {
    const { t } = this.ctx;
    return el("div", {}, [
      el("label", { class: "images-font" }, [
        el("span", { class: "muted", text: t("ReleaseScreen", "Game text font") }),
        this.gameFontSelect(),
      ]),
      this.gameFontPreview(),
      el("p", {
        class: "muted",
        text: t(
          "ReleaseScreen",
          "The game's text is drawn in this font, into the game or from beside it.",
        ),
      }),
      canBuild(this.engine(), this.gameFont)
        ? null
        : el("p", {
            class: "severity-warning",
            text: t("ReleaseScreen", "Choose the game text font before building."),
          }),
    ]);
  }

  gameFontSelect() {
    const { t } = this.ctx;
    const select = el("select", {
      class: "images-font-select",
      "aria-label": t("ReleaseScreen", "Game text font"),
    });
    const options = gameFontOptions(this.gameFont.font, this.gameFonts, t).map((option) =>
      el("option", { value: option.value, text: option.label, disabled: option.disabled }),
    );
    if (this.gameFonts === null) {
      options.push(el("option", { disabled: true, text: t("ReleaseScreen", "Loading fonts…") }));
    }
    select.replaceChildren(...options);
    select.value = this.gameFont.font ? this.gameFont.font.path : "";
    select.addEventListener("change", () => {
      if (!select.value) return;
      select.disabled = true;
      this.settle(async () => {
        this.gameFont = await this.ctx.bridge.save_game_font(select.value);
      });
    });
    return select;
  }

  // Lettered in the face read from its own file, not one the webview might
  // substitute by name: what the game will draw.
  gameFontPreview() {
    const { t } = this.ctx;
    const path = this.gameFont.font?.path ?? null;
    if (!path) return null;
    if (path !== this.preview.path) this.loadPreview(path);
    const lines = this.gameFont.sample ?? [];
    if (!lines.length) {
      return el("p", {
        class: "muted",
        text: t("ReleaseScreen", "No translated lines to preview the font with yet."),
      });
    }
    if (this.preview.status === "failed") {
      return el("p", {
        class: "muted",
        text: t("ReleaseScreen", "This font cannot be previewed here."),
      });
    }
    const box = el(
      "div",
      {
        class: "game-font-preview",
        "data-state": this.preview.status,
        "aria-label": t("ReleaseScreen", "Font preview"),
      },
      lines.map((line) => el("div", { text: line })),
    );
    if (this.preview.family) box.style.fontFamily = `"${this.preview.family}"`;
    return box;
  }

  loadPreview(path) {
    this.preview = { path, family: null, status: "loading" };
    loadFace(this.ctx.bridge, path).then((family) => {
      if (this.preview.path !== path) return;
      this.preview = { path, family, status: family ? "ready" : "failed" };
      this.render();
    });
  }

  uifField() {
    const { bridge, t } = this.ctx;
    const button = (text, change) =>
      el("button", { class: "btn btn-sm", type: "button", text, onClick: () => this.settle(change) });
    return el("div", {}, [
      el("label", { class: "images-font" }, [
        el("span", { class: "muted", text: t("ReleaseScreen", "UIF winmm.dll") }),
        el("span", { class: "mono", text: uifLabel(this.uif, t) }),
        button(t("ReleaseScreen", "Choose…"), async () => {
          const picked = await bridge.choose_file(this.uif.path ?? "", "dll");
          if (!picked) return;
          this.uif = await bridge.save_uif_dll(picked);
        }),
        this.uif.path
          ? button(t("ReleaseScreen", "Clear"), async () => {
              this.uif = await bridge.save_uif_dll(null);
            })
          : null,
      ]),
    ]);
  }

  renderRail() {
    const { t } = this.ctx;
    const stateOf = (index) =>
      index === this.stepIndex ? "active" : index < this.stepIndex ? "done" : "pending";
    this.rail.replaceChildren(
      ...WIZARD_STEPS.map((step, index) =>
        el("div", { class: "release-step", "data-state": stateOf(index) }, [
          el("span", { class: "release-step-n", text: String(index + 1) }),
          el("span", { class: "release-step-label", text: t("ReleaseScreen", step.label) }),
        ]),
      ),
    );
  }

  renderCheck() {
    const { t } = this.ctx;
    const project = this.ctx.appState.project;
    if (!project) {
      return [el("p", { class: "muted", text: t("ReleaseScreen", "No project is open.") })];
    }
    const figures = checkFigures(this.unitCounts);
    const shown = (key) => (figures ? String(figures[key]) : "--");
    return [
      el("p", {
        class: "page-description",
        text: t("ReleaseScreen", "Confirm what is about to be packaged."),
      }),
      el("div", { class: "metric-grid" }, [
        summaryRow(t("ReleaseScreen", "Engine"), project.engine ?? "--"),
        summaryRow(t("ReleaseScreen", "Text units"), shown("total")),
        summaryRow(t("ReleaseScreen", "Untranslated"), shown("untranslated")),
        summaryRow(t("ReleaseScreen", "Review pending"), shown("pending")),
        summaryRow(t("ReleaseScreen", "Reviewed"), shown("reviewed")),
      ]),
      figures?.untranslated > 0
        ? el("p", {
            class: "muted",
            text: t(
              "ReleaseScreen",
              "{count} unit(s) are untranslated and ship as original text.",
              { count: figures.untranslated },
            ),
          })
        : null,
    ];
  }

  renderOptions() {
    const { t } = this.ctx;
    const livemaker = this.engine() === "livemaker";
    return [
      el("p", {
        class: "page-description",
        text: t(
          "ReleaseScreen",
          livemaker
            ? "Builds a patch that compiles the translation when players apply it to their original game."
            : "Builds a patch that installs the translation when players apply it to their original game.",
        ),
      }),
      livemaker ? this.gameFontField() : null,
      el("p", { class: "muted mono", text: t("ReleaseScreen", "Output: dist/<timestamp>/") }),
      livemaker ? this.advancedSettings() : null,
    ];
  }

  // Every change on the options step renders it anew; the disclosure stays as
  // the translator left it.
  advancedSettings() {
    return el(
      "details",
      {
        class: "wizard-advanced",
        open: this.advancedOpen,
        ontoggle: (event) => {
          this.advancedOpen = event.currentTarget.open;
        },
      },
      [el("summary", { text: this.ctx.t("ReleaseScreen", "Advanced settings") }), this.uifField()],
    );
  }

  renderBuild() {
    const terminal = el("pre", {
      class: "mono release-terminal",
      "aria-label": this.ctx.t("ReleaseScreen", "Build log"),
      text: this.ctx.jobLog.lines().join("\n"),
    });
    this.terminal = terminal;
    requestAnimationFrame(() => {
      terminal.scrollTop = terminal.scrollHeight;
    });
    return [terminal];
  }

  // Follows the end of the log as it grows, unless it was scrolled back to read.
  showLog() {
    const terminal = this.terminal;
    if (!terminal?.isConnected) return;
    const following =
      terminal.scrollTop + terminal.clientHeight >= terminal.scrollHeight - 4;
    terminal.textContent = this.ctx.jobLog.lines().join("\n");
    if (following) terminal.scrollTop = terminal.scrollHeight;
  }

  renderDone() {
    const { t } = this.ctx;
    const result = this.result;
    if (!result) {
      return [el("p", { class: "muted", text: t("ReleaseScreen", "Nothing has been built yet.") })];
    }
    return [
      el("p", {
        class: "page-description",
        text: t("ReleaseScreen", "The release is ready."),
      }),
      el("div", { class: "metric-grid" }, [
        summaryRow(t("ReleaseScreen", "Translated units"), String(result.translated)),
        summaryRow(t("ReleaseScreen", "Source files"), String(result.files)),
        summaryRow(t("ReleaseScreen", "Proxy characters"), String(result.proxy_total)),
        summaryRow(t("ReleaseScreen", "Quality findings"), String(result.qa_issues ?? 0)),
      ]),
      ...this.doneNotes(result),
      pathField(t, "Patch for players: {path}", result.patcher),
      el("p", {
        class: "muted",
        text: t(
          "ReleaseScreen",
          "Players apply this patch to their original game and restore the original with the same patch.",
        ),
      }),
    ];
  }

  doneNotes(result) {
    const { t } = this.ctx;
    return [
      result.qa_issues
        ? el("p", {
            class: "muted",
            text: t(
              "ReleaseScreen",
              "Quality checks found {count} thing(s) to look at. The build goes ahead regardless; the quality check screen lists them all.",
              { count: result.qa_issues },
            ),
          })
        : null,
      result.proxy_repaired
        ? el("p", {
            class: "severity-warning",
            text: t(
              "ReleaseScreen",
              "{count} character(s) were sharing a donor and have been remapped. Anything built from the earlier proxy map drew them wrong; rebuild the game font.",
              { count: result.proxy_repaired },
            ),
          })
        : null,
    ];
  }

  stepBody(key) {
    if (key === "check") return this.renderCheck();
    if (key === "options") return this.renderOptions();
    if (key === "build") return this.renderBuild();
    return this.renderDone();
  }

  render() {
    const { t } = this.ctx;
    const step = WIZARD_STEPS[this.stepIndex];
    this.renderRail();
    this.stepTitle.textContent = t("ReleaseScreen", step.label);
    this.status.textContent = t("ReleaseScreen", "Step {step} of {total}", {
      step: this.stepIndex + 1,
      total: WIZARD_STEPS.length,
    });
    this.body.dataset.step = step.key;
    this.body.replaceChildren(...this.stepBody(step.key).filter(Boolean));
    this.nextButton.textContent = t("ReleaseScreen", NEXT_LABELS[step.key]);
    // Back stays live while building: the job owns its own cancel button in the
    // job bar, and leaving the step must never become a dead end if the job
    // failed to start at all.
    this.nextButton.disabled =
      step.key === "build" || (step.key === "options" && !canBuild(this.engine(), this.gameFont));
    this.backButton.disabled = this.stepIndex === 0;
    this.goButton.hidden = step.key !== "done" || !this.result?.artifact_dir;
  }

  goTo(key) {
    this.stepIndex = WIZARD_STEPS.findIndex((step) => step.key === key);
    this.render();
  }

  next() {
    const key = WIZARD_STEPS[this.stepIndex].key;
    if (key === "check") {
      this.goTo("options");
    } else if (key === "options") {
      this.goTo("build");
      this.ctx.startJob("release");
    } else if (key === "done") {
      this.result = null;
      this.goTo("check");
      this.loadUnitCounts().catch((err) => this.ctx.showError(err));
    }
  }

  back() {
    if (this.stepIndex > 0) this.goTo(WIZARD_STEPS[this.stepIndex - 1].key);
  }

  async openFolder() {
    if (!this.result?.artifact_dir) return;
    this.goButton.disabled = true;
    try {
      await this.ctx.bridge.open_release_folder(this.result.artifact_dir);
    } catch (err) {
      this.ctx.showError(err);
    } finally {
      this.goButton.disabled = false;
    }
  }

  onEvent(event) {
    if (event.job !== "release") return;
    if (event.type === "succeeded") {
      this.result = event.detail;
      this.goTo("done");
    } else if (event.type === "failed" || event.type === "cancelled") {
      this.goTo("options");
    }
  }
}
