// image_editor_modal.js -- the overlay the canvas editor is mounted in.
//
// Two screens open the same editor on the same asset: the images workspace
// picks the picture off its inventory, the strings queue reaches it from the
// unit whose text was read off it.

import { el } from "./dom.js";
import { confirmDialog } from "./confirm.js";
import { describeDialog, returnFocus, trapFocus } from "./dialog.js";
import { mountEditor } from "./canvas_editor.js";
import { icon } from "./image_editor/icons.js";

export function createImageEditorHost(ctx) {
  const { bridge, t } = ctx;
  let session = null;

  async function close(force = false) {
    if (session === null) return;
    if (!force && session.controller?.isDirty()) {
      const discard = await confirmDialog(ctx, {
        message: t("ImageEditor", "Discard unsaved changes?"),
        confirmText: t("ImageEditor", "Discard"),
      });
      // The dialog yields, so the editor may already be gone by now.
      if (!discard || session === null) return;
    }
    session.controller?.destroy();
    session.overlay.remove();
    returnFocus(session.opener);
    session = null;
  }

  function open(path, { onSaved } = {}) {
    close(true);
    const opener = document.activeElement;
    const closeButton = el(
      "button",
      {
        class: "editor-shell-close",
        type: "button",
        title: `${t("ImageEditor", "Close")} (Esc)`,
        "aria-label": t("ImageEditor", "Close"),
      },
      [icon("close")],
    );
    const host = el("div");
    const panel = el("div", { class: "modal-panel image-editor-panel" }, [closeButton, host]);
    const overlay = el("div", { class: "modal-overlay" }, [panel]);
    // Escape is the editor's own: it stops playback, then drops the selection,
    // then closes, so the overlay must not answer it a second time.
    describeDialog(overlay, {
      label: t("ImageEditor", "Image editor - {name}", { name: path.split(/[\\/]/).at(-1) }),
    });
    trapFocus(overlay);
    session = { overlay, controller: null, opener };
    document.body.append(overlay);
    closeButton.addEventListener("click", () => close(false));
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) close(false);
    });
    mountEditor(host, path, bridge, {
      t,
      onSaved,
      onRequestClose: (force) => close(force),
    }).then((controller) => {
      if (session?.overlay === overlay) session.controller = controller;
      else controller.destroy();
    });
  }

  return { open, close };
}
