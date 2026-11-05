import { afterEach, beforeEach, expect, test } from "bun:test";

import { choiceDialog } from "../../src/bokucho/web/assets/confirm.js";
import { hideDialog, showDialog, trapFocus } from "../../src/bokucho/web/assets/dialog.js";

// Only what el() and the dialogs touch: attributes, children, listeners, focus.
class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.attributes = new Map();
    this.children = [];
    this.listeners = new Map();
    this.parent = null;
    this.hidden = false;
    this.tabIndex = 0;
    this.textContent = "";
    this.className = "";
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  get id() {
    return this.getAttribute("id");
  }

  get firstElementChild() {
    return this.children[0] ?? null;
  }

  get isConnected() {
    let node = this;
    while (node.parent) node = node.parent;
    return node === globalThis.document.body;
  }

  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }

  remove() {
    this.parent.children = this.parent.children.filter((node) => node !== this);
    this.parent = null;
  }

  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  press(key, extra = {}) {
    const event = { key, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra };
    for (const listener of this.listeners.get("keydown") ?? []) listener(event);
    return event;
  }

  contains(node) {
    return node === this || this.children.some((child) => child.contains(node));
  }

  focus() {
    globalThis.document.activeElement = this;
  }

  getClientRects() {
    return [{}];
  }

  querySelectorAll() {
    const found = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (child.tag === "button") found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
}

const saved = globalThis.document;
let opener;

beforeEach(() => {
  const body = new FakeNode("body");
  globalThis.document = { body, createElement: (tag) => new FakeNode(tag), activeElement: body };
  opener = new FakeNode("button");
  body.append(opener);
  opener.focus();
});

afterEach(() => {
  globalThis.document = saved;
});

function openDialog() {
  const answer = choiceDialog({
    message: "Delete the term Easy?",
    choices: [
      { key: false, label: "Cancel" },
      { key: true, label: "Delete", primary: true },
    ],
  });
  const overlay = document.body.children.at(-1);
  return { answer, overlay, panel: overlay.firstElementChild };
}

test("a confirmation announces itself as a modal dialog named by its message", () => {
  const { panel } = openDialog();
  const message = panel.children[0];

  expect(panel.getAttribute("role")).toBe("alertdialog");
  expect(panel.getAttribute("aria-modal")).toBe("true");
  expect(panel.getAttribute("aria-labelledby")).toBe(message.id);
  expect(message.textContent).toBe("Delete the term Easy?");
});

test("the primary button takes the keyboard, and Escape hands it back to the opener", async () => {
  const { answer, overlay, panel } = openDialog();
  const [, actions] = panel.children;

  expect(document.activeElement).toBe(actions.children[1]);
  overlay.press("Escape");

  expect(await answer).toBeNull();
  expect(overlay.isConnected).toBe(false);
  expect(document.activeElement).toBe(opener);
});

test("answering with a button also gives focus back", async () => {
  const { answer, panel } = openDialog();
  const [, actions] = panel.children;

  actions.children[1].listeners.get("click")[0]();

  expect(await answer).toBe(true);
  expect(document.activeElement).toBe(opener);
});

test("Tab wraps from the last control to the first, and Shift+Tab the other way", () => {
  const overlay = new FakeNode("div");
  const [first, last] = [new FakeNode("button"), new FakeNode("button")];
  overlay.append(first, last);
  document.body.append(overlay);
  trapFocus(overlay);

  last.focus();
  const forward = overlay.press("Tab");
  expect(forward.defaultPrevented).toBe(true);
  expect(document.activeElement).toBe(first);

  const backward = overlay.press("Tab", { shiftKey: true });
  expect(backward.defaultPrevented).toBe(true);
  expect(document.activeElement).toBe(last);
});

test("a dialog that holds focus on its panel keeps Tab and Shift+Tab inside", () => {
  const overlay = new FakeNode("div");
  const panel = new FakeNode("div");
  const [first, last] = [new FakeNode("button"), new FakeNode("button")];
  panel.tabIndex = -1;
  panel.append(first, last);
  overlay.append(panel);
  document.body.append(overlay);
  trapFocus(overlay);

  panel.focus();
  const backward = overlay.press("Tab", { shiftKey: true });
  expect(backward.defaultPrevented).toBe(true);
  expect(document.activeElement).toBe(last);

  panel.focus();
  overlay.press("Tab");
  expect(document.activeElement).toBe(first);
});

test("Tab from a control the trap does not list is left to the browser", () => {
  const overlay = new FakeNode("div");
  const panel = new FakeNode("div");
  const [first, summary, last] = [new FakeNode("button"), new FakeNode("summary"), new FakeNode("button")];
  panel.append(first, summary, last);
  overlay.append(panel);
  document.body.append(overlay);
  trapFocus(overlay);

  summary.focus();

  expect(overlay.press("Tab").defaultPrevented).toBe(false);
  expect(overlay.press("Tab", { shiftKey: true }).defaultPrevented).toBe(false);
});

test("Tab inside the dialog is left to the browser", () => {
  const overlay = new FakeNode("div");
  const [first, middle, last] = [new FakeNode("button"), new FakeNode("button"), new FakeNode("button")];
  overlay.append(first, middle, last);
  document.body.append(overlay);
  trapFocus(overlay);

  middle.focus();

  expect(overlay.press("Tab").defaultPrevented).toBe(false);
});

test("a dialog that is shown and hidden returns the keyboard to what opened it", () => {
  const overlay = new FakeNode("div");
  const field = new FakeNode("input");
  overlay.append(field);
  document.body.append(overlay);
  overlay.hidden = true;

  showDialog(overlay, field);
  expect(overlay.hidden).toBe(false);
  expect(document.activeElement).toBe(field);

  hideDialog(overlay);
  expect(overlay.hidden).toBe(true);
  expect(document.activeElement).toBe(opener);
});
