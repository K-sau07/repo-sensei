import { describe, expect, it } from "vitest";

import { matchesProducedKey, shortcut, SHORTCUTS, toHotkey } from "./shortcut-registry";
import { formatShortcut } from "./shortcuts";

describe("shortcut registry", () => {
  it("has unique ids and unique key bindings", () => {
    expect(new Set(SHORTCUTS.map((s) => s.id)).size).toBe(SHORTCUTS.length);
    expect(new Set(SHORTCUTS.map((s) => s.keys)).size).toBe(SHORTCUTS.length);
  });

  it("only uses keys the hint formatter understands", () => {
    for (const { keys } of SHORTCUTS) {
      for (const step of formatShortcut(keys, "apple")) {
        expect(step.length).toBeGreaterThan(0);
      }
    }
  });

  it("translates sequences for react-hotkeys-hook", () => {
    expect(toHotkey("g h")).toBe("g>h");
    expect(toHotkey("mod+k")).toBe("mod+k");
    expect(toHotkey("?")).toBe("?");
  });

  it("matches punctuation by the produced character", () => {
    expect(matchesProducedKey("?")).toBe(true);
    expect(matchesProducedKey("[")).toBe(true);
    expect(matchesProducedKey("k")).toBe(false);
    expect(matchesProducedKey("mod+k")).toBe(false);
  });

  it("looks shortcuts up by id", () => {
    expect(shortcut("command").keys).toBe("mod+k");
  });
});
