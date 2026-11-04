import { afterEach, describe, expect, it } from "vitest";

import {
  APPEARANCE_BOOT_SCRIPT,
  APPEARANCE_CSS_KEY,
  APPEARANCE_KEY,
  APPEARANCE_STYLE_ID,
  appearanceCss,
  DEFAULT_APPEARANCE,
  saveAppearance,
  subscribeAppearance,
} from "./appearance";

function runBootScript() {
  // The inline <head> script, executed exactly as the browser would run it.
  const script = document.createElement("script");
  script.textContent = APPEARANCE_BOOT_SCRIPT;
  document.head.append(script);
  script.remove();
}

function clear() {
  const root = document.documentElement;
  delete root.dataset.density;
  delete root.dataset.motion;
  delete root.dataset.transparency;
  document.getElementById(APPEARANCE_STYLE_ID)?.remove();
}

afterEach(() => {
  localStorage.clear();
  clear();
});

describe("the appearance boot script", () => {
  it("restores exactly what was saved, palette included", () => {
    const choice = { ...DEFAULT_APPEARANCE, accentHue: 150, density: "compact" as const };
    saveAppearance(choice);
    const saved = Object.fromEntries(Object.entries(document.documentElement.dataset));
    clear();

    runBootScript();
    expect(Object.fromEntries(Object.entries(document.documentElement.dataset))).toEqual(saved);
    expect(document.getElementById(APPEARANCE_STYLE_ID)?.textContent).toBe(appearanceCss(choice));
  });

  it.each(["not json", '{"density": 3}', "null"])("survives stored value %s", (stored) => {
    localStorage.setItem(APPEARANCE_KEY, stored);
    runBootScript();
    expect(document.documentElement.dataset.density).toBe(
      stored === "not json" ? undefined : "comfortable",
    );
  });

  it("puts no stored palette with markup in it on the page", () => {
    localStorage.setItem(APPEARANCE_CSS_KEY, "</style><script>alert(1)</script>");
    runBootScript();
    expect(document.getElementById(APPEARANCE_STYLE_ID)).toBeNull();
  });

  it("takes a choice saved in another tab, and replaces a palette an older version made", () => {
    const choice = { ...DEFAULT_APPEARANCE, accentHue: 255, density: "compact" as const };
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(choice));
    localStorage.setItem(APPEARANCE_CSS_KEY, "[data-theme]{--brand: red;}"); // stale
    const unsubscribe = subscribeAppearance(() => undefined);
    window.dispatchEvent(new StorageEvent("storage", { key: APPEARANCE_KEY }));
    unsubscribe();
    expect(document.documentElement.dataset.density).toBe("compact");
    expect(document.getElementById(APPEARANCE_STYLE_ID)?.textContent).toBe(appearanceCss(choice));
    expect(localStorage.getItem(APPEARANCE_CSS_KEY)).toBe(appearanceCss(choice));
  });
});
