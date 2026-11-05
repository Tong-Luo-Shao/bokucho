// dom.js -- tiny DOM builder + HTML escaping shared by the screen modules.
//
// Every attribute value here goes through `setAttribute`/`textContent`, never
// `innerHTML`, so building elements through `el()` carries no XSS risk. The
// one place raw HTML is assembled (glossary-underlined source text) escapes
// first and is confined to strings.js.

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else {
      node.setAttribute(key, value === true ? "" : value);
    }
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

let idCounter = 0;

export function uniqueId(prefix) {
  idCounter += 1;
  return `${prefix}-${idCounter}`;
}

export function labelControl(label, control) {
  if (!["INPUT", "SELECT", "TEXTAREA"].includes(control.tagName)) return label;
  control.id ||= uniqueId("control");
  label.setAttribute("for", control.id);
  return label;
}

export function clear(node) {
  node.replaceChildren();
}

export function setChildren(node, children = []) {
  node.replaceChildren(...[].concat(children).filter((child) => child !== null && child !== undefined));
}

export function escapeHtml(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
