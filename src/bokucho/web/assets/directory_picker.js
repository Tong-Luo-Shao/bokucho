import { el } from "./dom.js";

export function createDirectoryPicker(
  ctx,
  {
    id,
    label,
    placeholder = "",
    browseText,
    files = false,
    onError = ctx.showError,
  },
) {
  const { bridge } = ctx;
  const input = el("input", {
    id,
    type: "text",
    placeholder,
    "aria-label": label,
  });

  const button = el("button", {
    class: "btn directory-picker-button",
    type: "button",
    text: browseText,
    "aria-label": `${browseText}: ${label}`,
  });
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const current = input.value.trim();
      // `files` fields point at a game, which is picked as its executable; the
      // engine unpacks that into the project. The text input still accepts an
      // already-unpacked directory for projects configured that way.
      const selected = files
        ? await bridge.choose_file(current)
        : await bridge.choose_directory(current);
      if (selected) {
        input.value = selected;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }
    } catch (error) {
      onError(error);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });

  return {
    element: el("div", { class: "directory-picker" }, [input, button]),
    input,
    button,
  };
}
