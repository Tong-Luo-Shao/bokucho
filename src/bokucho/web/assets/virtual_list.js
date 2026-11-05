// virtual_list.js -- fixed-row-height windowed list over a paged data source.
//
// The strings queue and image inventory render only the visible slice (plus a
// small overscan) as absolutely-positioned rows inside a full-height spacer.

export function createVirtualList({ viewport, rowHeight, overscan = 6, loadSlice, renderRow, onRender }) {
  const spacer = document.createElement("div");
  spacer.style.position = "relative";
  viewport.replaceChildren(spacer);
  viewport.style.overflowY = "auto";
  viewport.style.position = "relative";

  let total = 0;
  const rendered = new Map(); // index -> { node, row }
  let pending = false;
  let requestToken = 0;
  let wantedFocus = null;

  // A row that holds the keyboard is kept even when it scrolls out of range, so
  // wheeling away from the active row does not drop the reader's focus.
  function holdsFocus(node) {
    return node.contains?.(document.activeElement) ?? false;
  }

  function placeFocus() {
    const entry = wantedFocus === null ? undefined : rendered.get(wantedFocus);
    if (!entry) return;
    wantedFocus = null;
    entry.node.focus({ preventScroll: true });
  }

  function visibleRange() {
    const scrollTop = viewport.scrollTop;
    const viewHeight = viewport.clientHeight || rowHeight * 10;
    const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
    const last = Math.min(total - 1, Math.ceil((scrollTop + viewHeight) / rowHeight) + overscan);
    return { first, last };
  }

  async function renderVisible() {
    const { first, last } = visibleRange();
    for (const [index, entry] of rendered) {
      if ((index < first || index > last) && !holdsFocus(entry.node)) {
        entry.node.remove();
        rendered.delete(index);
      }
    }
    if (last < first) return;
    const token = ++requestToken;
    const rows = await loadSlice(first, last - first + 1);
    if (token !== requestToken) return; // a newer slice request superseded this one
    rows.forEach((row, offset) => {
      const index = first + offset;
      if (rendered.has(index)) return;
      const node = renderRow(row, index);
      node.style.position = "absolute";
      node.style.top = `${index * rowHeight}px`;
      node.style.left = "0";
      node.style.right = "0";
      node.style.height = `${rowHeight}px`;
      spacer.append(node);
      rendered.set(index, { node, row });
    });
    onRender?.();
    placeFocus();
  }

  function scheduleRender() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      renderVisible();
    });
  }

  viewport.addEventListener("scroll", scheduleRender);

  async function redraw() {
    const { first, last } = visibleRange();
    const token = ++requestToken;
    const rows = last < first ? [] : await loadSlice(first, last - first + 1);
    if (token !== requestToken) return;

    const focused = [...rendered].find(([, { node }]) => holdsFocus(node));
    const restoreAt = focused && wantedFocus === null ? focused[0] : null;
    if (restoreAt !== null) wantedFocus = restoreAt;
    const replacements = rows.map((row, offset) => {
      const index = first + offset;
      const node = renderRow(row, index);
      node.style.position = "absolute";
      node.style.top = `${index * rowHeight}px`;
      node.style.left = "0";
      node.style.right = "0";
      node.style.height = `${rowHeight}px`;
      return { index, node, row };
    });
    const fragment = document.createDocumentFragment();
    for (const replacement of replacements) fragment.append(replacement.node);
    for (const { node } of rendered.values()) node.remove();
    rendered.clear();
    spacer.append(fragment);
    for (const { index, node, row } of replacements) {
      rendered.set(index, { node, row });
    }
    onRender?.();
    placeFocus();
    if (restoreAt !== null && wantedFocus === restoreAt) wantedFocus = null;
  }

  function updateRendered(updateRow) {
    for (const [index, { node, row }] of rendered) {
      updateRow(node, row, index);
    }
  }

  // Scrolls the row into view and gives it the keyboard; a row that is not on
  // the page yet is focused as soon as it is drawn.
  function focusIndex(index) {
    const top = index * rowHeight;
    const bottom = top + rowHeight;
    if (top < viewport.scrollTop) viewport.scrollTop = top;
    else if (bottom > viewport.scrollTop + viewport.clientHeight) {
      viewport.scrollTop = bottom - viewport.clientHeight;
    }
    wantedFocus = index;
    if (rendered.has(index)) placeFocus();
    else scheduleRender();
  }

  // The list is told its length again after every change to what it shows, so
  // the reader stays where they were; a shorter list brings the scroll in itself.
  function setTotal(nextTotal) {
    total = nextTotal;
    spacer.style.height = `${total * rowHeight}px`;
    redraw();
  }

  return { setTotal, refresh: redraw, redraw, updateRendered, focusIndex };
}
