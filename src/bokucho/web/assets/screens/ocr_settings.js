import { el } from "../dom.js";

const DETECTOR_ID = "PaddlePaddle/PP-OCRv6_medium_det_safetensors";
const RECOGNIZER_ID = "PaddlePaddle/PP-OCRv6_medium_rec_safetensors";
const ERASER_ID = "Sanster/big-lama";

function field(label, input, hint = null) {
  const children = [el("label", { text: label }), input];
  // A hint that is rewritten as the form changes arrives as the node holding
  // it, not as the words it says right now.
  if (hint) {
    children.push(
      typeof hint === "string" ? el("span", { class: "field-hint", text: hint }) : hint,
    );
  }
  return el("div", { class: "field-row" }, children);
}

function formatMegabytes(value) {
  const bytes = Number(value);
  return Number.isFinite(bytes) ? `${(bytes / 1_000_000).toFixed(2)} MB` : "--";
}

function checkpointPanel(t, title, modelId) {
  const details = el("dl", { class: "ocr-model-details" });
  return {
    details,
    root: el("section", { class: "card ocr-settings-panel" }, [
      el("div", { class: "ocr-settings-panel-heading" }, [
        el("div", {}, [
          el("h2", { class: "card-title", text: t("OcrSettings", title) }),
          el("p", { class: "field-hint mono", text: modelId }),
        ]),
      ]),
      details,
    ]),
  };
}

function thresholdInput(id) {
  return el("input", { id, type: "number", min: "0", max: "1", step: "0.01", disabled: true });
}

function validThreshold(value) {
  return Number.isFinite(value) && value >= 0 && value <= 1;
}

function renderCheckpoint(t, target, checkpoint) {
  // The eraser is pinned by hash rather than by a hub revision.
  const rows = [
    ...(checkpoint.revision
      ? [[t("OcrSettings", "Revision"), checkpoint.revision.slice(0, 8)]]
      : []),
    [t("OcrSettings", "Checkpoint download"), formatMegabytes(checkpoint.download_bytes)],
    [
      t("OcrSettings", "Cache status"),
      checkpoint.cached ? t("OcrSettings", "Ready offline") : t("OcrSettings", "Not cached"),
    ],
  ];
  target.replaceChildren(
    ...rows.map(([label, value]) =>
      el("div", {}, [el("dt", { text: label }), el("dd", { text: value })]),
    ),
  );
}

export function mount(container, ctx) {
  const form = new OcrSettingsForm(ctx);
  container.replaceChildren(form.element);
  const load = () => ctx.bridge.ocr_status().then((result) => form.render(result));
  const unsubscribe = ctx.onEvent((event) => {
    if (["succeeded", "failed", "cancelled"].includes(event.type) && String(event.job).startsWith("images-ocr")) {
      load().catch((error) => ctx.showError(error));
    }
  });
  load().catch((error) => ctx.showError(error));
  return { refresh: load, unmount: unsubscribe };
}

class OcrSettingsForm {
  constructor(ctx) {
    const { t } = ctx;
    this.ctx = ctx;
    this.detector = checkpointPanel(t, "Text detection", DETECTOR_ID);
    this.recognizer = checkpointPanel(t, "Text recognition", RECOGNIZER_ID);
    this.eraser = checkpointPanel(t, "Text erasing", ERASER_ID);
    this.summaryTitle = el("strong", { text: t("OcrSettings", "Loading OCR settings...") });
    this.summaryMeta = el("span", { class: "muted" });
    this.cacheBadge = el("span", { class: "ocr-cache-badge" });
    this.batchMode = el("select", { id: "ocr-batch-mode", disabled: true }, [
      el("option", { value: "auto", text: t("OcrSettings", "Automatic (recommended)") }),
      el("option", { value: "custom", text: t("OcrSettings", "Custom") }),
    ]);
    this.batchSize = el("input", {
      id: "ocr-batch-size", type: "number", min: "1", max: "64", step: "1", disabled: true,
    });
    this.batchHint = el("span", { class: "field-hint" });
    this.detectionThreshold = thresholdInput("ocr-detection-threshold");
    this.recognitionThreshold = thresholdInput("ocr-recognition-threshold");
    this.saveMessage = el("p", { class: "muted ocr-save-message" });
    this.saveButton = el("button", {
      class: "btn btn-primary",
      type: "button",
      text: t("OcrSettings", "Save OCR settings"),
      disabled: true,
    });
    this.element = this.layout();
    this.batchMode.addEventListener("change", () => this.renderBatchControls());
    this.saveButton.addEventListener("click", () => this.save());
  }

  layout() {
    const { t } = this.ctx;
    return el("div", { class: "ocr-settings-screen" }, [
      el("div", { class: "page-header" }, [
        el("div", { class: "heading" }, [
          el("h1", { class: "page-title", text: t("OcrSettings", "OCR settings") }),
          el("p", {
            class: "page-description",
            text: t("OcrSettings", "PP-OCRv6 detects text, then recognizes accepted regions."),
          }),
        ]),
      ]),
      el("section", { class: "ocr-settings-summary" }, [
        el("div", { class: "ocr-settings-summary-copy" }, [this.summaryTitle, this.summaryMeta]),
        this.cacheBadge,
      ]),
      el("div", { class: "ocr-settings-grid" }, [
        this.detector.root,
        this.recognizer.root,
        this.eraser.root,
        this.performancePanel(),
        this.confidencePanel(),
      ]),
      el("div", { class: "button-bar ocr-settings-actions" }, [
        this.saveMessage,
        el("div", { class: "spacer" }),
        this.saveButton,
      ]),
    ]);
  }

  performancePanel() {
    const { t } = this.ctx;
    return el("section", { class: "card ocr-settings-panel" }, [
      el("div", { class: "ocr-settings-panel-heading" }, [
        el("h2", { class: "card-title", text: t("OcrSettings", "Performance") }),
      ]),
      field(t("OcrSettings", "Batch mode"), this.batchMode),
      field(t("OcrSettings", "Images per batch"), this.batchSize, this.batchHint),
    ]);
  }

  confidencePanel() {
    const { t } = this.ctx;
    return el("section", { class: "card ocr-settings-panel" }, [
      el("div", { class: "ocr-settings-panel-heading" }, [
        el("h2", { class: "card-title", text: t("OcrSettings", "Confidence") }),
      ]),
      field(
        t("OcrSettings", "Detection threshold"),
        this.detectionThreshold,
        t("OcrSettings", "Boxes must score strictly above this value."),
      ),
      field(
        t("OcrSettings", "Recognition threshold"),
        this.recognitionThreshold,
        t("OcrSettings", "Text must score strictly above this value."),
      ),
    ]);
  }

  renderBatchControls() {
    const automatic = this.batchMode.value === "auto";
    this.batchSize.disabled = automatic;
    if (automatic) this.batchSize.value = "8";
    this.batchHint.textContent = this.ctx.t("OcrSettings", "Recommended: 8 images.");
  }

  render(result) {
    const { t } = this.ctx;
    renderCheckpoint(t, this.detector.details, result.detector);
    renderCheckpoint(t, this.recognizer.details, result.recognizer);
    renderCheckpoint(t, this.eraser.details, result.eraser);
    this.batchMode.value = result.batch_size_mode;
    this.batchSize.value = String(result.batch_size);
    this.detectionThreshold.value = String(result.detection_threshold);
    this.recognitionThreshold.value = String(result.recognition_threshold);
    this.renderSummary(result);
    for (const control of [this.batchMode, this.detectionThreshold, this.recognitionThreshold, this.saveButton]) {
      control.disabled = false;
    }
    this.renderBatchControls();
  }

  renderSummary(result) {
    const { t } = this.ctx;
    const pendingBytes = (result.runtime_cached ? 0 : result.runtime_download_bytes)
      + (result.detector.cached ? 0 : result.detector.download_bytes)
      + (result.recognizer.cached ? 0 : result.recognizer.download_bytes);
    this.summaryTitle.textContent = t("OcrSettings", "Fixed PP-OCRv6 pipeline");
    this.summaryMeta.textContent = `${DETECTOR_ID} / ${RECOGNIZER_ID}`;
    this.cacheBadge.textContent = pendingBytes
      ? t("OcrSettings", "Downloads {size} on next scan", {
          size: formatMegabytes(pendingBytes),
        })
      : t("OcrSettings", "Ready offline");
    this.cacheBadge.classList.toggle("is-ready", pendingBytes === 0);
  }

  showMessage(text, className) {
    this.saveMessage.textContent = text;
    this.saveMessage.className = `${className} ocr-save-message`;
  }

  // The form values, or the reason they cannot be saved.
  requestedSettings() {
    const { t } = this.ctx;
    const customBatch = this.batchMode.value === "custom";
    const batch = customBatch ? Number(this.batchSize.value) : 0;
    const detection = Number(this.detectionThreshold.value);
    const recognition = Number(this.recognitionThreshold.value);
    if (customBatch && (!Number.isInteger(batch) || batch < 1 || batch > 64)) {
      return { problem: t("OcrSettings", "Custom batch size must be between 1 and 64.") };
    }
    if (!validThreshold(detection) || !validThreshold(recognition)) {
      return { problem: t("OcrSettings", "Confidence thresholds must be between 0 and 1.") };
    }
    return { batch, detection, recognition };
  }

  async save() {
    const { t } = this.ctx;
    const requested = this.requestedSettings();
    if (requested.problem) {
      this.showMessage(requested.problem, "severity-error");
      return;
    }
    this.saveButton.disabled = true;
    this.showMessage(t("OcrSettings", "Saving..."), "muted");
    try {
      const result = await this.ctx.bridge.save_ocr_configuration(
        requested.batch,
        requested.detection,
        requested.recognition,
      );
      this.render(result);
      this.saveMessage.textContent = t("OcrSettings", "OCR settings saved.");
    } catch (error) {
      this.showMessage(error.message, "severity-error");
      this.saveButton.disabled = false;
    }
  }
}
