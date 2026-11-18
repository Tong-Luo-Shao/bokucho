// transform_dialog.js -- which conversion Transform runs, and how many lines it changes.
//
// The choice is saved into project.toml before the job starts, so the job, the
// CLI and the next dialog all read the same [[transform]] entries.

import { el, uniqueId } from "../dom.js";
import { describeDialog, returnFocus, trapFocus } from "../dialog.js";

// The first converts vocabulary too (软件 becomes 軟體); the second only characters.
const OFFERED = [
  ["s2twp", "Simplified → Traditional"],
  ["s2tw", "Simplified → Traditional, characters only"],
];

// A config written into project.toml by hand stays on offer, so opening the
// dialog and converting does not quietly replace it.
export function transformOptions(savedConfig, say) {
  const options = OFFERED.map(([value, label]) => ({ value, label: say(label) }));
  if (!OFFERED.some(([value]) => value === savedConfig)) {
    options.push({ value: savedConfig, label: say("{config} (from project.toml)", { config: savedConfig }) });
  }
  return options;
}

export function transformCountText(count, say) {
  if (count === null) return say("Counting…");
  if (count === 0) return say("Nothing to convert.");
  return say("{count} line(s) will change.", { count });
}

// A count reads every string file, so only one runs at a time: choices made
// while it runs wait, and only the last of them is counted next. An answer is
// shown only when no newer choice is waiting, and never after close().
export function oneCountAtATime(count, { onCount, onError }) {
  let running = false;
  let waiting = false;
  let next;
  let closed = false;

  async function run() {
    running = true;
    while (waiting && !closed) {
      const choice = next;
      waiting = false;
      try {
        const answer = await count(choice);
        if (!waiting && !closed) onCount(answer);
      } catch (err) {
        if (!waiting && !closed) onError(err);
      }
    }
    running = false;
  }

  return {
    ask(choice) {
      next = choice;
      waiting = true;
      if (!running) run();
    },
    close() {
      closed = true;
      waiting = false;
    },
  };
}

// Resolves true once the choice is saved and the conversion should run.
export async function openTransformDialog(ctx) {
  const say = (source, vars) => ctx.t("TransformDialog", source, vars);
  let saved;
  try {
    saved = await ctx.bridge.transform_choice();
  } catch (err) {
    ctx.showError(err);
    return false;
  }

  const group = uniqueId("transform-config");
  const radios = transformOptions(saved.config, say).map((option) => ({
    option,
    input: el("input", { type: "radio", name: group }),
  }));
  const quotesBox = el("input", { type: "checkbox" });
  const count = el("p", { class: "transform-count", role: "status" });
  const cancelButton = el("button", { class: "btn", type: "button", text: ctx.t("Common", "Cancel") });
  const convertButton = el("button", { class: "btn btn-primary", type: "button", text: say("Convert"), disabled: true });
  const titleId = uniqueId("transform-title");
  const overlay = el("div", { class: "modal-overlay" }, [
    el("div", { class: "modal-panel transform-panel", tabindex: "-1" }, [
      el("h2", { class: "card-title", id: titleId, text: say("Transform text") }),
      el("div", { class: "transform-options" }, [
        ...radios.map(({ option, input }) => el("label", { class: "transform-option" }, [input, option.label])),
        el("label", { class: "transform-option transform-quotes" }, [quotesBox, say("Curly quotes → 「」『』")]),
      ]),
      el("p", { class: "field-hint", text: say("Reviewed lines are left as they are. This cannot be undone.") }),
      count,
      el("div", { class: "modal-actions" }, [cancelButton, convertButton]),
    ]),
  ]);
  for (const { option, input } of radios) input.checked = option.value === saved.config;
  quotesBox.checked = saved.quotes;
  const opener = document.activeElement;

  const chosen = () => ({
    config: radios.find(({ input }) => input.checked).option.value,
    quotes: quotesBox.checked,
  });
  const counter = oneCountAtATime(
    async ({ config, quotes }) => (await ctx.bridge.count_transform(config, quotes)).changed,
    {
      onCount(changed) {
        count.textContent = transformCountText(changed, say);
        convertButton.disabled = changed === 0;
      },
      onError(err) {
        count.textContent = "";
        ctx.showError(err);
      },
    },
  );

  // Until the count for the choice on screen arrives, Convert stays off.
  function refreshCount() {
    convertButton.disabled = true;
    count.textContent = transformCountText(null, say);
    counter.ask(chosen());
  }

  return new Promise((resolve) => {
    function close(converted) {
      counter.close();
      overlay.remove();
      returnFocus(opener);
      resolve(converted);
    }

    async function convert() {
      const { config, quotes } = chosen();
      convertButton.disabled = true;
      cancelButton.disabled = true;
      try {
        await ctx.bridge.save_transform_choice(config, quotes);
        close(true);
      } catch (err) {
        ctx.showError(err);
        cancelButton.disabled = false;
        convertButton.disabled = false;
      }
    }

    describeDialog(overlay, { labelledBy: titleId });
    trapFocus(overlay, { onEscape: () => close(false) });
    cancelButton.addEventListener("click", () => close(false));
    convertButton.addEventListener("click", convert);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) close(false);
    });
    for (const { input } of radios) input.addEventListener("change", refreshCount);
    quotesBox.addEventListener("change", refreshCount);
    document.body.append(overlay);
    (radios.find(({ input }) => input.checked)?.input ?? cancelButton).focus();
    refreshCount();
  });
}
