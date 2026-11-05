import { expect, test } from "bun:test";

import {
  engineOptions,
  engineValueForDetection,
  gameNameFromPath,
} from "../../src/bokucho/web/assets/screens/create_project.js";

test("a selected game executable defaults the project name to its folder", () => {
  expect(gameNameFromPath("C:\\Games\\Dark Witch 5\\Game.exe")).toBe("Dark Witch 5");
  expect(gameNameFromPath("C:/Games/Dark Witch 5")).toBe("Dark Witch 5");
});

test("RPG Maker adapter is shown as separate concrete engine choices", () => {
  expect(engineOptions([
    "livemaker", "renpy", "rpgmaker-mv", "rpgmaker-mz", "rpgmaker-vx-ace",
  ])).toEqual([
    { value: "livemaker", engine: "livemaker", label: "LiveMaker / LiveNovel" },
    { value: "renpy", engine: "renpy", label: "Ren'Py" },
    { value: "rpgmaker-mv", engine: "rpgmaker-mv", label: "RPG Maker MV" },
    { value: "rpgmaker-mz", engine: "rpgmaker-mz", label: "RPG Maker MZ" },
    {
      value: "rpgmaker-vx-ace",
      engine: "rpgmaker-vx-ace",
      label: "RPG Maker VX Ace",
    },
  ]);
});

test("RPG Maker detection selects its concrete engine choice", () => {
  expect(engineValueForDetection({
    engine: "rpgmaker-mz",
    evidence: ["Game.rmmzproject", "data/System.json"],
  })).toBe("rpgmaker-mz");
  expect(engineValueForDetection({
    engine: "rpgmaker-mv",
    evidence: ["Game.rpgproject", "www/data/System.json"],
  })).toBe("rpgmaker-mv");
  expect(engineValueForDetection({
    engine: "rpgmaker-vx-ace",
    evidence: ["Game.rgss3a", "Data/System.rvdata2"],
  })).toBe("rpgmaker-vx-ace");
});

test("XP and VX are offered by name", () => {
  expect(engineOptions(["rpgmaker-xp", "rpgmaker-vx"])).toEqual([
    { value: "rpgmaker-xp", engine: "rpgmaker-xp", label: "RPG Maker XP" },
    { value: "rpgmaker-vx", engine: "rpgmaker-vx", label: "RPG Maker VX" },
  ]);
});

test("a detected XP game keeps its engine", () => {
  expect(engineValueForDetection({ engine: "rpgmaker-xp", evidence: ["Game.rgssad"] })).toBe("rpgmaker-xp");
});

test("a packed XP or VX archive names the game by its folder", () => {
  expect(gameNameFromPath("D:/Games/勇者/Game.rgssad")).toBe("勇者");
  expect(gameNameFromPath("D:/Games/勇者/Game.rgss2a")).toBe("勇者");
});
