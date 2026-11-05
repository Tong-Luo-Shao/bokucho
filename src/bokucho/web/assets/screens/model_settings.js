// model_settings.js -- provider-first interface selection. Connection details
// and generation limits remain behind explicit interface actions.

import { clear, el, labelControl, setChildren, uniqueId } from "../dom.js";
import { confirmDialog } from "../confirm.js";
import { describeDialog, hideDialog, showDialog, trapFocus } from "../dialog.js";
import { providerMark } from "../provider_icons.js";

const THINKING_LEVELS = ["none", "low", "medium", "high", "xhigh", "max"];
// The dropdown entry that hands the model name back to a text field.
const MODEL_CUSTOM = "__custom__";

export async function storedApiKeysText(bridge, item) {
  if (!item || item.credential_count === 0) return "";
  const result = await bridge.provider_interface_keys(item.id);
  return result.api_keys.join("\n");
}

function firstApiKey(value) {
  for (const line of value.split("\n")) {
    for (const part of line.split(",")) {
      const key = part.trim();
      if (key) return key;
    }
  }
  return "";
}

// A stored key belongs to the address it was saved for: pointing the dialog at
// another address empties the box rather than carrying the keys along.
export function keysForAddress(storedKeys, storedAddress, address) {
  const endpoint = (value) => (value ?? "").trim().replace(/\/+$/, "").toLowerCase();
  return endpoint(storedAddress) === endpoint(address) ? storedKeys : "";
}

export function createApiKeyModelLookup(loadModels, delay = 500) {
  let pending = null;
  return (value) => {
    if (pending !== null) clearTimeout(pending);
    pending = null;
    const key = firstApiKey(value);
    if (!key) return;
    pending = setTimeout(() => {
      pending = null;
      loadModels(key);
    }, delay);
  };
}

function field(label, input, hint = null) {
  const children = [labelControl(el("label", { text: label }), input), input];
  if (hint) children.push(el("span", { class: "field-hint", text: hint }));
  return el("div", { class: "field-row" }, children);
}

function quotaPill(label, value, unlimited) {
  return el("span", {
    class: "quota-pill",
    text: `${label} ${value || unlimited}`,
  });
}

function numberInput(min, step, max = null) {
  return el("input", { type: "number", min, max, step });
}

function modalShell(id, title, children, onClose) {
  const dialog = el("div", { id, class: "modal-overlay", hidden: true }, [
    el("div", { class: "modal-panel interface-dialog" }, [title, ...children]),
  ]);
  describeDialog(dialog, { labelledBy: title.id });
  trapFocus(dialog, { onEscape: onClose });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) onClose();
  });
  return dialog;
}

function actionButton(label, action, destructive = false) {
  return el("button", {
    class: destructive
      ? "interface-action interface-action-destructive"
      : "interface-action",
    type: "button",
    text: label,
    "data-action": action,
  });
}

async function runAction(action, errorTarget) {
  try {
    await action();
  } catch (error) {
    errorTarget.textContent = error.message;
  }
}

function showFailure(target, error) {
  target.textContent = error.message;
  target.hidden = false;
}

export function mount(container, ctx) {
  const screen = new ModelSettingsScreen(ctx);
  container.replaceChildren(screen.element);
  const refresh = () => screen.refresh();
  refresh().catch((error) => ctx.showError(error));
  return { refresh, unmount() {} };
}

// The model list is a menu of whatever the provider reports, with a last
// entry for the models it does not: a preview id, or an endpoint with no
// catalogue at all. It is one split button: the value half opens the menu of
// what the provider reported, the chevron half re-asks the provider first.
class ModelPicker {
  constructor(ctx, onFetch) {
    const { t } = ctx;
    this.ctx = ctx;
    this.selected = MODEL_CUSTOM;
    this.value = el("span", { class: "model-split-value mono" });
    this.trigger = el("button", {
      class: "model-split-primary",
      type: "button",
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      onClick: () => this.setMenuOpen(this.menu.hidden),
    }, [this.value]);
    this.fetchButton = el("button", {
      class: "model-split-toggle",
      type: "button",
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      title: t("ModelSettings", "Fetch models"),
      "aria-label": t("ModelSettings", "Fetch models"),
      onClick: onFetch,
    }, [el("span", { class: "provider-chevron", text: "v" })]);
    this.menu = el("div", { class: "model-menu", role: "menu", hidden: true });
    this.split = el("div", {
      class: "model-split-button",
      role: "group",
      "aria-label": t("ModelSettings", "Model name"),
    }, [this.trigger, this.fetchButton]);
    this.custom = el("input", {
      type: "text",
      autocomplete: "off",
      placeholder: t("ModelSettings", "Model identifier"),
      hidden: true,
    });
    // The menu sits in the field column, in flow. The dialog panel scrolls, and
    // it clips anything positioned outside that flow at its own edge.
    this.element = el("div", { class: "model-field" }, [this.split, this.menu, this.custom]);
    this.element.addEventListener("keydown", (event) => this.onKeydown(event));
    // Leaving the control at all closes it, which covers a click outside as well
    // as tabbing past it.
    this.element.addEventListener("focusout", (event) => {
      if (!this.element.contains(event.relatedTarget)) this.setMenuOpen(false);
    });
  }

  current() {
    return this.selected === MODEL_CUSTOM ? this.custom.value.trim() : this.selected;
  }

  setMenuOpen(open) {
    this.menu.hidden = !open;
    this.split.classList.toggle("is-open", open);
    for (const half of [this.trigger, this.fetchButton]) {
      half.setAttribute("aria-expanded", String(open));
    }
    if (open) {
      const checked = this.menu.querySelector('[aria-checked="true"]');
      (checked ?? this.menu.firstElementChild)?.focus();
    }
  }

  choose(model) {
    this.selected = model;
    this.value.textContent =
      model === MODEL_CUSTOM ? this.ctx.t("ModelSettings", "Type a model name") : model;
    this.custom.hidden = model !== MODEL_CUSTOM;
    for (const item of this.menu.children) {
      item.setAttribute("aria-checked", String(item.dataset.model === model));
    }
  }

  moveFocus(step) {
    const items = [...this.menu.children];
    if (!items.length) return;
    const at = items.indexOf(document.activeElement);
    const next =
      at < 0
        ? (step > 0 ? 0 : items.length - 1)
        : (at + step + items.length) % items.length;
    items[next].focus();
  }

  setModels(models, selected) {
    const known = [...new Set([...models, selected].filter(Boolean))];
    const entries = [
      ...known.map((model) => [model, model]),
      [MODEL_CUSTOM, this.ctx.t("ModelSettings", "Type a model name")],
    ];
    setChildren(this.menu, entries.map(([model, label]) => this.menuItem(model, label)));
    this.choose(selected || known[0] || MODEL_CUSTOM);
  }

  menuItem(model, label) {
    return el("button", {
      class: "model-menu-item",
      type: "button",
      role: "menuitemradio",
      "aria-checked": "false",
      tabindex: "-1",
      text: label,
      "data-model": model,
      onClick: () => {
        this.choose(model);
        this.setMenuOpen(false);
        if (model === MODEL_CUSTOM) this.custom.focus();
        else this.trigger.focus();
      },
    });
  }

  onKeydown(event) {
    if (event.key === "Escape") {
      if (this.menu.hidden) return;
      event.preventDefault();
      event.stopPropagation();
      this.setMenuOpen(false);
      this.trigger.focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (this.menu.hidden) this.setMenuOpen(true);
      else this.moveFocus(event.key === "ArrowDown" ? 1 : -1);
    } else if (!this.menu.hidden && (event.key === "Home" || event.key === "End")) {
      event.preventDefault();
      const items = [...this.menu.children];
      (event.key === "Home" ? items[0] : items.at(-1))?.focus();
    }
  }
}

class ConnectionDialog {
  constructor(ctx, { close, saved }) {
    const { t } = ctx;
    this.ctx = ctx;
    this.saved = saved;
    this.target = null;
    // The keys the dialog filled in, and the address they were stored for.
    this.storedKeys = "";
    this.storedAddress = "";
    this.title = el("h2", { class: "modal-title", id: uniqueId("interface-connection-title") });
    this.provider = el("div", { class: "interface-provider-lock" });
    this.name = el("input", { type: "text" });
    this.baseUrl = el("input", { type: "url", placeholder: "provider.example/v1" });
    this.apiKey = el("textarea", {
      rows: "4",
      autocomplete: "off",
      spellcheck: "false",
      class: "mono api-key-input",
      placeholder: t("ModelSettings", "One key per line, or separated by commas"),
    });
    // The chevron half re-asks the provider, then shows the list either way: a
    // refusal is already spelled out in the dialog by loadModels.
    this.picker = new ModelPicker(ctx, async () => {
      await this.loadModels().catch(() => {});
      this.picker.setMenuOpen(true);
    });
    this.credentialHint = el("span", { class: "field-hint" });
    this.error = el("p", { class: "severity-error", hidden: true });
    this.clearKeysButton = el("button", {
      class: "btn btn-sm",
      type: "button",
      text: t("ModelSettings", "Clear keys"),
      hidden: true,
      onClick: () => this.clearKeys(),
    });
    this.element = modalShell("interface-connection-dialog", this.title, this.fields(close), () => close(this.element));
    const lookUpModels = createApiKeyModelLookup((key) => {
      this.loadModels(key).catch(() => {});
    });
    this.baseUrl.addEventListener("input", () => this.followAddress());
    this.apiKey.addEventListener("input", () => lookUpModels(this.apiKey.value));
  }

  fields(close) {
    const { t } = this.ctx;
    return [
      this.provider,
      field(t("ModelSettings", "Interface name"), this.name),
      field(t("ModelSettings", "Base URL"), this.baseUrl),
      field(
        t("ModelSettings", "API keys"),
        this.apiKey,
        t(
          "ModelSettings",
          "Adds a key. Several are used in parallel, each with its own quota.",
        ),
      ),
      field(t("ModelSettings", "Model name"), this.picker.element),
      el("div", { class: "model-fetch-row" }, [this.credentialHint, this.clearKeysButton]),
      this.error,
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", text: t("Common", "Cancel"), onClick: () => close(this.element) }),
        el("button", { class: "btn btn-primary", type: "button", text: t("Common", "Save"), onClick: () => this.save() }),
      ]),
    ];
  }

  // Listing models takes one key, not the list: the first one typed, or none,
  // which falls back to whatever the interface already holds.
  firstKey() {
    return firstApiKey(this.apiKey.value);
  }

  storedCountHint(count) {
    this.credentialHint.textContent = this.ctx.t("ModelSettings", "{count} API keys stored", { count });
  }

  open(provider, item) {
    const { t } = this.ctx;
    this.target = { provider, item };
    this.title.textContent = item
      ? t("ModelSettings", "Edit interface")
      : t("ModelSettings", "Add interface");
    this.provider.textContent = provider.label;
    this.name.value = item?.name ?? provider.label;
    this.baseUrl.value = item?.base_url ?? provider.base_url ?? "";
    this.apiKey.value = "";
    this.storedKeys = "";
    this.storedAddress = this.baseUrl.value;
    this.picker.setMenuOpen(false);
    this.picker.setModels([], item?.model ?? "");
    if (item) this.storedCountHint(item.credential_count);
    else this.credentialHint.textContent = t("ModelSettings", "Enter a key to fetch models.");
    this.clearKeysButton.hidden = !item || item.credential_count === 0;
    this.error.hidden = true;
    return item && item.credential_count > 0 ? item : null;
  }

  async fillStoredKeys(item) {
    try {
      this.storedKeys = await storedApiKeysText(this.ctx.bridge, item);
      this.apiKey.value = this.storedKeys;
    } catch (error) {
      showFailure(this.error, error);
    }
    this.loadModels().catch(() => {});
  }

  // A reply that arrives after the dialog closed, or after another key was
  // typed, is not this dialog's any more.
  stale(expectedKey) {
    return this.element.hidden || (expectedKey && expectedKey !== this.firstKey());
  }

  async loadModels(expectedKey = null) {
    if (!this.target || this.stale(expectedKey)) return;
    this.picker.fetchButton.disabled = true;
    this.error.hidden = true;
    try {
      const { provider, item } = this.target;
      // Whatever is on screen, never what was last saved: a key typed into the
      // dialog is used before it is stored, and a blank key falls back to the
      // one already held for the interface being edited.
      const result = await this.ctx.bridge.discover_provider_models(
        provider.key,
        this.baseUrl.value.trim() || null,
        expectedKey || this.firstKey() || null,
        "",
        item?.id ?? null,
      );
      if (this.stale(expectedKey)) return;
      this.picker.setModels(result.models, this.picker.current());
      this.credentialHint.textContent = this.ctx.t(
        "ModelSettings",
        "{count} models available.",
        { count: result.models.length },
      );
    } catch (error) {
      if (this.stale(expectedKey)) return;
      showFailure(this.error, error);
      throw error;
    } finally {
      this.picker.fetchButton.disabled = false;
    }
  }

  followAddress() {
    if (!this.storedKeys) return;
    const address = this.baseUrl.value.trim() || this.target?.provider.base_url;
    const kept = keysForAddress(this.storedKeys, this.storedAddress, address);
    if (!kept && this.apiKey.value === this.storedKeys) {
      this.apiKey.value = "";
      this.credentialHint.textContent = this.ctx.t(
        "ModelSettings",
        "Stored keys stay with their own address. Enter a key for this one.",
      );
    } else if (kept && this.apiKey.value === "") {
      this.apiKey.value = this.storedKeys;
      this.storedCountHint(this.target.item.credential_count);
    }
  }

  async save() {
    if (!this.target) return;
    this.error.hidden = true;
    try {
      const { provider, item } = this.target;
      await this.ctx.bridge.save_provider_interface(
        {
          id: item?.id ?? null,
          name: this.name.value.trim(),
          provider: provider.key,
          model: this.picker.current(),
          base_url: this.baseUrl.value.trim() || null,
          api_key_env: "",
        },
        this.apiKey.value || null,
      );
      hideDialog(this.element);
      await this.saved();
    } catch (error) {
      showFailure(this.error, error);
    }
  }

  async clearKeys() {
    const item = this.target?.item;
    if (!item) return;
    try {
      const result = await this.ctx.bridge.clear_provider_interface_keys(item.id);
      this.target = { ...this.target, item: result.interface };
      this.storedKeys = await storedApiKeysText(this.ctx.bridge, result.interface);
      this.apiKey.value = this.storedKeys;
      this.storedCountHint(result.interface.credential_count);
      this.clearKeysButton.hidden = true;
      await this.saved({ keepOpen: true });
    } catch (err) {
      this.ctx.showError(err);
    }
  }
}

class LimitsDialog {
  constructor(ctx, { close, saved }) {
    const { t } = ctx;
    this.ctx = ctx;
    this.saved = saved;
    this.target = null;
    this.title = el("h2", { class: "modal-title", id: uniqueId("interface-limits-title") });
    this.rpm = numberInput("0", "1");
    this.tpm = numberInput("0", "1");
    this.rpd = numberInput("0", "1");
    this.batchSize = numberInput("0", "1");
    this.maxRequestTokens = numberInput("0", "256");
    this.contextUnits = numberInput("0", "1");
    this.concurrency = numberInput("1", "1", "16");
    this.temperature = numberInput("0", "0.1", "2");
    this.thinking = el("select", {}, THINKING_LEVELS.map((level) => el("option", { value: level, text: level })));
    this.error = el("p", { class: "severity-error", hidden: true });
    this.element = modalShell("interface-limits-dialog", this.title, [
      el("div", { class: "limit-field-grid" }, this.fields()),
      el("p", {
        class: "field-hint",
        text: t(
          "ModelSettings",
          "Token counts come from tiktoken, an estimate for providers with their own tokenizer.",
        ),
      }),
      this.error,
      el("div", { class: "modal-actions" }, [
        el("button", { class: "btn", type: "button", text: t("Common", "Cancel"), onClick: () => close(this.element) }),
        el("button", { class: "btn btn-primary", type: "button", text: t("Common", "Save"), onClick: () => this.save() }),
      ]),
    ], () => close(this.element));
  }

  fields() {
    const { t } = this.ctx;
    const unlimitedHint = t("ModelSettings", "0 means unlimited.");
    return [
      field(t("ModelSettings", "Requests per minute (RPM)"), this.rpm, unlimitedHint),
      field(t("ModelSettings", "Input tokens per minute (TPM)"), this.tpm, unlimitedHint),
      field(t("ModelSettings", "Requests per day (RPD)"), this.rpd, unlimitedHint),
      field(
        t("ModelSettings", "Tokens per request"),
        this.maxRequestTokens,
        t("ModelSettings", "A sentence stays whole; 0 sets no budget."),
      ),
      field(
        t("ModelSettings", "Maximum sentences per request"),
        this.batchSize,
        t("ModelSettings", "0 leaves the token budget in charge."),
      ),
      field(
        t("ModelSettings", "Preceding lines as context"),
        this.contextUnits,
        t("ModelSettings", "Sent read-only so a task starting mid-scene has continuity."),
      ),
      field(
        t("ModelSettings", "Concurrent requests per key"),
        this.concurrency,
        t("ModelSettings", "Multiplied by the number of keys; RPM and TPM still apply."),
      ),
      field(t("ModelSettings", "Temperature"), this.temperature),
      field(t("ModelSettings", "Thinking level"), this.thinking),
    ];
  }

  open(item) {
    this.target = item;
    this.title.textContent = this.ctx.t(
      "ModelSettings",
      "Adjust parameters for {name}",
      { name: item.name },
    );
    this.rpm.value = item.rpm ?? 0;
    this.tpm.value = item.tpm ?? 0;
    this.rpd.value = item.rpd ?? 0;
    this.batchSize.value = item.batch_size ?? 20;
    this.maxRequestTokens.value = item.max_request_tokens ?? 10240;
    this.contextUnits.value = item.context_units ?? 3;
    this.concurrency.value = item.concurrency ?? 1;
    this.temperature.value = item.temperature ?? "";
    this.thinking.value = item.thinking_level ?? "none";
    this.error.hidden = true;
  }

  async save() {
    if (!this.target) return;
    this.error.hidden = true;
    try {
      await this.ctx.bridge.update_provider_interface_parameters(
        this.target.id,
        Number(this.rpm.value) || 0,
        Number(this.tpm.value) || 0,
        Number(this.rpd.value) || 0,
        this.temperature.value === "" ? null : Number(this.temperature.value),
        this.thinking.value,
        Number(this.batchSize.value) || 0,
        Number(this.maxRequestTokens.value) || 0,
        Number(this.contextUnits.value) || 0,
        Number(this.concurrency.value) || 1,
      );
      hideDialog(this.element);
      await this.saved();
    } catch (error) {
      showFailure(this.error, error);
    }
  }
}

class ModelSettingsScreen {
  constructor(ctx) {
    const { t } = ctx;
    this.ctx = ctx;
    this.catalog = { providers: [], interfaces: [], active_interface_id: null };
    this.openProviderKey = null;
    // Saving rebuilds the provider panel, and the button that opened a dialog
    // goes with it; the same action is found again in the new panel.
    this.openerSelector = ".add-interface-button";
    this.activeSummary = el("section", { class: "active-interface-summary" });
    this.providerSelector = el("div", { class: "provider-selector", role: "list" });
    this.providerPanel = el("section", { class: "provider-interface-panel", hidden: true });
    const close = (dialog) => {
      hideDialog(dialog);
      this.restoreOpener();
    };
    const saved = async ({ keepOpen = false } = {}) => {
      await this.refresh();
      if (!keepOpen) this.restoreOpener();
    };
    this.connection = new ConnectionDialog(ctx, { close, saved });
    this.limits = new LimitsDialog(ctx, { close, saved });
    // The active interface is the banner; the providers are a rail beside the
    // interfaces they hold, so choosing one never scrolls the other away.
    this.element = el("div", { class: "model-screen" }, [
      this.activeSummary,
      el("div", { class: "model-body" }, [
        el("nav", { class: "model-rail", "aria-label": t("ModelSettings", "Providers") }, [
          el("div", { class: "model-rail-head", text: t("ModelSettings", "Providers") }),
          this.providerSelector,
        ]),
        el("div", { class: "model-main" }, [this.providerPanel]),
      ]),
      this.connection.element,
      this.limits.element,
    ]);
  }

  rememberOpener() {
    const action = document.activeElement?.dataset?.action;
    this.openerSelector = action ? `[data-action="${action}"]` : ".add-interface-button";
  }

  restoreOpener() {
    this.providerPanel.querySelector(this.openerSelector)?.focus();
  }

  async openConnection(provider, item) {
    const withKeys = this.connection.open(provider, item);
    this.rememberOpener();
    showDialog(this.connection.element, this.connection.name);
    if (withKeys) await this.connection.fillStoredKeys(withKeys);
  }

  openLimits(item) {
    this.limits.open(item);
    this.rememberOpener();
    showDialog(this.limits.element, this.limits.rpm);
  }

  providerFor(key) {
    return this.catalog.providers.find((provider) => provider.key === key);
  }

  interfaceFor(id) {
    return this.catalog.interfaces.find((item) => item.id === id);
  }

  interfacesFor(providerKey) {
    return this.catalog.interfaces.filter((item) => item.provider === providerKey);
  }

  activeInterface() {
    return this.interfaceFor(this.catalog.active_interface_id);
  }

  async refresh() {
    this.catalog = await this.ctx.bridge.provider_interfaces();
    if (
      this.openProviderKey === null ||
      !this.catalog.providers.some((provider) => provider.key === this.openProviderKey)
    ) {
      this.openProviderKey =
        this.activeInterface()?.provider ?? this.catalog.providers[0]?.key ?? null;
    }
    this.renderActiveSummary();
    this.renderProviderSelector();
    this.renderProviderPanel();
  }

  renderActiveSummary() {
    const { t } = this.ctx;
    const active = this.activeInterface();
    const label = el("span", { class: "active-interface-label", text: t("ModelSettings", "Active interface") });
    if (!active) {
      setChildren(this.activeSummary, el("div", {}, [
        label,
        el("strong", { text: t("ModelSettings", "No interface selected") }),
      ]));
      return;
    }
    const providerLabel = this.providerFor(active.provider)?.label ?? active.provider;
    setChildren(this.activeSummary, [
      el("div", { class: "active-interface-identity" }, [
        providerMark(active.provider, providerLabel, "active-interface-mark"),
        el("div", {}, [
          label,
          el("strong", { text: active.name }),
          el("span", { class: "muted", text: `${providerLabel} / ${active.model}` }),
        ]),
      ]),
      el("div", { class: "quota-strip" }, [
        quotaPill("RPM", active.rpm, t("ModelSettings", "unlimited")),
        quotaPill("TPM", active.tpm, t("ModelSettings", "unlimited")),
        quotaPill("RPD", active.rpd, t("ModelSettings", "unlimited")),
      ]),
    ]);
  }

  renderProviderSelector() {
    setChildren(this.providerSelector, this.catalog.providers.map((provider) => this.providerButton(provider)));
  }

  providerButton(provider) {
    const configured = this.interfacesFor(provider.key);
    const hasActive = configured.some((item) => item.id === this.catalog.active_interface_id);
    return el("button", {
      class: [
        "provider-trigger",
        this.openProviderKey === provider.key ? "is-open" : "",
        hasActive ? "has-active" : "",
      ].filter(Boolean).join(" "),
      type: "button",
      role: "listitem",
      onClick: () => {
        this.openProviderKey = this.openProviderKey === provider.key ? null : provider.key;
        this.renderProviderSelector();
        this.renderProviderPanel();
      },
    }, [
      providerMark(provider.key, provider.label, "provider-monogram"),
      el("span", { class: "provider-trigger-label", text: provider.label }),
      el("span", { class: "provider-interface-count", text: String(configured.length) }),
      el("span", { class: "provider-chevron", text: "v" }),
    ]);
  }

  renderProviderPanel() {
    const { t } = this.ctx;
    clear(this.providerPanel);
    this.providerPanel.hidden = this.openProviderKey === null;
    if (this.openProviderKey === null) return;
    const provider = this.providerFor(this.openProviderKey);
    const configured = this.interfacesFor(this.openProviderKey);
    const selected =
      configured.find((item) => item.id === this.catalog.active_interface_id) ??
      configured[0] ??
      null;
    const select = this.interfaceSelect(configured, selected);
    const testResult = el("span", { class: "interface-test-result muted" });
    const chosen = () => this.interfaceFor(select.value) ?? selected;
    this.providerPanel.append(
      el("div", { class: "provider-panel-header" }, [
        el("h2", { text: provider.label }),
        el("button", {
          class: "btn btn-primary add-interface-button",
          type: "button",
          text: t("ModelSettings", "Add interface"),
          onClick: () => this.openConnection(provider, null),
        }),
      ]),
      el("div", { class: "interface-picker-row" }, [select, testResult]),
      el("div", { class: "interface-action-menu" }, this.interfaceActions(provider, chosen, selected, testResult)),
    );
  }

  interfaceSelect(configured, selected) {
    const { t } = this.ctx;
    return el("select", {
      class: "interface-select",
      "aria-label": t("ModelSettings", "Interface name"),
      disabled: configured.length === 0,
    }, configured.length
      ? configured.map((item) =>
          el("option", {
            value: item.id,
            text: `${item.name} / ${item.model}`,
            selected: item.id === selected?.id,
          }),
        )
      : [el("option", { text: t("ModelSettings", "No configured interfaces") })]);
  }

  interfaceActions(provider, chosen, selected, testResult) {
    const { t } = this.ctx;
    const actions = [
      [t("ModelSettings", "Use interface"), "use-interface", (item) => this.useInterface(item, testResult)],
      [t("ModelSettings", "Test interface"), "test-interface", (item) => this.testInterface(item, testResult)],
      [t("ModelSettings", "Edit interface"), "edit-interface", (item) => this.openConnection(provider, item)],
      [t("ModelSettings", "Adjust parameters"), "adjust-parameters", (item) => this.openLimits(item)],
      [t("ModelSettings", "Delete interface"), "delete-interface", (item) => this.deleteInterface(item, testResult)],
    ];
    return actions.map(([label, action, run]) => {
      const button = actionButton(label, action, action === "delete-interface");
      button.disabled = selected === null;
      button.addEventListener("click", () => {
        const item = chosen();
        if (item) run(item);
      });
      return button;
    });
  }

  useInterface(item, testResult) {
    return runAction(async () => {
      await this.ctx.bridge.activate_provider_interface(item.id);
      await this.refresh();
    }, testResult);
  }

  testInterface(item, testResult) {
    const { t } = this.ctx;
    testResult.textContent = t("ModelSettings", "Testing...");
    return runAction(async () => {
      const result = await this.ctx.bridge.test_provider_interface(item.id);
      testResult.textContent = t(
        "ModelSettings",
        "Connection ready; {count} models found.",
        { count: result.models },
      );
    }, testResult);
  }

  async deleteInterface(item, testResult) {
    const { t } = this.ctx;
    const confirmed = await confirmDialog(this.ctx, {
      message: t("ModelSettings", "Delete interface {name}?", { name: item.name }),
      confirmText: t("ModelSettings", "Delete"),
    });
    if (!confirmed) return;
    await runAction(async () => {
      await this.ctx.bridge.delete_provider_interface(item.id);
      await this.refresh();
    }, testResult);
  }
}
