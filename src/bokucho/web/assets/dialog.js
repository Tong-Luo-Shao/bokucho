// dialog.js -- what a modal overlay owes the keyboard and a screen reader
// beyond looking like one: a name, Tab that stays inside, and focus handed back
// to the control that opened it.

const TABBABLE = "a[href], button, input, select, textarea, [tabindex]";
const openers = new WeakMap();

function tabbables(root) {
  return [...root.querySelectorAll(TABBABLE)].filter(
    (node) => node.tabIndex >= 0 && !node.disabled && node.getClientRects().length > 0,
  );
}

export function describeDialog(overlay, { label, labelledBy, role = "dialog" }) {
  const panel = overlay.firstElementChild;
  panel.setAttribute("role", role);
  panel.setAttribute("aria-modal", "true");
  if (labelledBy) panel.setAttribute("aria-labelledby", labelledBy);
  else panel.setAttribute("aria-label", label);
  return panel;
}

export function trapFocus(overlay, { onEscape } = {}) {
  overlay.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && onEscape) {
      event.preventDefault();
      onEscape();
      return;
    }
    if (event.key !== "Tab") return;
    const nodes = tabbables(overlay);
    if (nodes.length === 0) return;
    const [first, last] = [nodes[0], nodes.at(-1)];
    const leaving = event.shiftKey ? document.activeElement === first : document.activeElement === last;
    const onPanel = document.activeElement === overlay.firstElementChild;
    if (leaving || onPanel || !overlay.contains(document.activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    }
  });
}

export function returnFocus(opener) {
  if (opener?.isConnected) opener.focus();
}

export function showDialog(overlay, focusTarget) {
  openers.set(overlay, document.activeElement);
  overlay.hidden = false;
  focusTarget?.focus();
}

export function hideDialog(overlay) {
  overlay.hidden = true;
  returnFocus(openers.get(overlay));
  openers.delete(overlay);
}
