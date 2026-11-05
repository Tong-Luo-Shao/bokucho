// list_keys.js -- where a listbox key takes the active row, shared by the
// strings queue and the image inventory.

export function listTarget(key, index, total, pageRows) {
  switch (key) {
    case "ArrowDown":
      return Math.min(index + 1, total - 1);
    case "ArrowUp":
      return Math.max(index - 1, 0);
    case "PageDown":
      return Math.min(index + pageRows, total - 1);
    case "PageUp":
      return Math.max(index - pageRows, 0);
    case "Home":
      return 0;
    case "End":
      return total - 1;
    default:
      return null;
  }
}
