// confirm.js -- in-app replacement for window.confirm.
//
// The webview renders a native confirm as a bar dropped from the top of the
// window, detached from the application it belongs to. This reuses the same
// centered `.modal-overlay` markup the create-project and interface dialogs
// already use, and resolves a promise instead of blocking.

import { el, uniqueId } from "./dom.js";
import { describeDialog, returnFocus } from "./dialog.js";

export function choiceDialog({ message, choices }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    const buttons = choices.map((choice) =>
      el("button", {
        class: choice.primary ? "btn btn-primary" : "btn",
        type: "button",
        text: choice.label,
        onclick: () => close(choice.key),
      }),
    );
    const messageId = uniqueId("confirm-message");
    const overlay = el("div", { class: "modal-overlay" }, [
      el("div", { class: "modal-panel confirm-panel" }, [
        el("p", { class: "confirm-message", id: messageId, text: message }),
        el("div", { class: "modal-actions" }, buttons),
      ]),
    ]);
    describeDialog(overlay, { labelledBy: messageId, role: "alertdialog" });

    function close(answer) {
      overlay.remove();
      returnFocus(opener);
      resolve(answer);
    }

    // Keys are handled on the overlay rather than the document: focus stays on
    // one of the buttons, so nothing underneath can answer the dialog by
    // accident, and Enter is whichever button the user is actually on.
    function onKey(event) {
      if (event.key === "Escape") close(null);
      else if (event.key === "Tab") {
        event.preventDefault();
        const at = buttons.indexOf(document.activeElement);
        const step = event.shiftKey ? -1 : 1;
        buttons[(at + step + buttons.length) % buttons.length].focus();
      }
    }

    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) close(null);
    });
    overlay.addEventListener("keydown", onKey);

    document.body.append(overlay);
    (buttons.find((_, index) => choices[index].primary) ?? buttons.at(-1))?.focus();
  });
}

export async function confirmDialog(ctx, { message, confirmText, cancelText }) {
  const { t } = ctx;
  const answer = await choiceDialog({
    message,
    choices: [
      { key: false, label: cancelText ?? t("Common", "Cancel") },
      { key: true, label: confirmText ?? t("Common", "Confirm"), primary: true },
    ],
  });
  return answer === true;
}
