import { expect, test } from "bun:test";

import { fontOptions } from "../../src/bokucho/web/assets/screens/images.js";

const t = (_context, text, values = {}) =>
  text.replace(/\{(\w+)\}/g, (_, key) => values[key]);

const DEFAULT = { path: "C:/Windows/Fonts/msjhbd.ttc", name: "Microsoft JhengHei Bold" };
const UNIFONT = { path: "C:/Windows/Fonts/unifont-all.ttf", name: "Unifont Smooth Medium" };
const MINGLIU = { path: "C:/Windows/Fonts/mingliu.ttc", name: "MingLiU" };
const GENYO = { path: "bundled:GenYoGothic2TW-R.otf", name: "GenYoGothic2 TW R" };
const MACHINE = "On this computer";

test("the default comes first, named for the face it stands for", () => {
  expect(fontOptions({ font: null, default: DEFAULT }, [UNIFONT], t)).toEqual([
    { value: "", label: "Default (Microsoft JhengHei Bold)" },
    { value: UNIFONT.path, label: UNIFONT.name, group: MACHINE },
  ]);
});

test("the fonts Bokucho ships are offered under their own heading", () => {
  expect(fontOptions({ font: null, default: DEFAULT }, [GENYO, UNIFONT], t)).toEqual([
    { value: "", label: "Default (Microsoft JhengHei Bold)" },
    { value: GENYO.path, label: GENYO.name, group: "Shipped with Bokucho" },
    { value: UNIFONT.path, label: UNIFONT.name, group: MACHINE },
  ]);
});

test("the chosen font is offered before the list arrives", () => {
  expect(fontOptions({ font: UNIFONT, default: DEFAULT }, null, t)).toEqual([
    { value: "", label: "Default (Microsoft JhengHei Bold)" },
    { value: UNIFONT.path, label: UNIFONT.name },
  ]);
});

test("the chosen font is offered once when the list holds it too", () => {
  const options = fontOptions({ font: UNIFONT, default: DEFAULT }, [MINGLIU, UNIFONT], t);
  expect(options.map((option) => option.value)).toEqual(["", MINGLIU.path, UNIFONT.path]);
});

test("a chosen font file that has gone is offered by its path", () => {
  const gone = { path: "D:/fonts/gone.ttf", name: null };
  expect(fontOptions({ font: gone, default: DEFAULT }, [], t)[1]).toEqual({
    value: gone.path,
    label: gone.path,
  });
});

test("with no font on the machine the default says so", () => {
  expect(fontOptions({ font: null, default: null }, [], t)[0]).toEqual({
    value: "",
    label: "Default",
  });
});
