import { describe, expect, it } from "vitest";

import { describeShortcut, detectPlatform, formatShortcut } from "./shortcuts";

const symbols = (shortcut: string, platform: "apple" | "other") =>
  formatShortcut(shortcut, platform).map((step) => step.map((key) => key.symbol));

describe("formatShortcut", () => {
  it.each([
    ["mod+k", "apple", [["⌘", "K"]]],
    ["mod+k", "other", [["Ctrl", "K"]]],
    ["shift+mod+p", "apple", [["⇧", "⌘", "P"]]],
    ["mod+shift+p", "other", [["Shift", "Ctrl", "P"]]],
    ["alt+enter", "apple", [["⌥", "↵"]]],
    ["alt+enter", "other", [["Alt", "Enter"]]],
    ["?", "apple", [["?"]]],
    ["g r", "other", [["G"], ["R"]]],
    ["esc", "apple", [["Esc"]]],
    ["mod+/", "other", [["Ctrl", "/"]]],
    ["f1", "other", [["F1"]]],
  ] as const)("%s on %s", (shortcut, platform, expected) => {
    expect(symbols(shortcut, platform)).toEqual(expected);
  });

  it("orders modifiers conventionally whatever the input order", () => {
    expect(symbols("mod+alt+shift+ctrl+x", "apple")).toEqual([["⌃", "⌥", "⇧", "⌘", "X"]]);
  });
});

describe("describeShortcut", () => {
  it.each([
    ["mod+k", "apple", "Command K"],
    ["mod+k", "other", "Control K"],
    ["g r", "apple", "G then R"],
    ["shift+?", "other", "Shift Question mark"],
  ] as const)("%s on %s reads as %s", (shortcut, platform, expected) => {
    expect(describeShortcut(shortcut, platform)).toBe(expected);
  });
});

describe("detectPlatform", () => {
  it.each([
    [{ platform: "MacIntel", userAgent: "" }, "apple"],
    [{ platform: "", userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0)" }, "apple"],
    [{ platform: "Win32", userAgent: "Mozilla/5.0 (Windows NT 10.0)" }, "other"],
    [{ platform: "Linux x86_64", userAgent: "Mozilla/5.0 (X11; Linux)" }, "other"],
  ])("%o is %s", (navigatorLike, expected) => {
    expect(detectPlatform(navigatorLike)).toBe(expected);
  });
});
