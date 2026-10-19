import { afterEach, describe, expect, it } from "vitest";

import {
  applyShellLayout,
  clampSidebarWidth,
  readPersisted,
  SHELL_BOOT_SCRIPT,
  SHELL_STORAGE_KEY,
} from "./shell-store";

function htmlState() {
  const root = document.documentElement;
  return {
    width: root.style.getPropertyValue("--sidebar-width"),
    sidebar: root.dataset.sidebar,
    rail: root.dataset.rail,
  };
}

function runBootScript() {
  // The inline <head> script, executed exactly as the browser would run it.
  const script = document.createElement("script");
  script.textContent = SHELL_BOOT_SCRIPT;
  document.head.append(script);
  script.remove();
}

afterEach(() => {
  localStorage.clear();
  const root = document.documentElement;
  root.style.removeProperty("--sidebar-width");
  delete root.dataset.sidebar;
  delete root.dataset.rail;
});

describe("shell layout persistence", () => {
  it.each([
    { sidebarWidth: 300, sidebarCollapsed: true, railOpen: true },
    { sidebarWidth: 240, sidebarCollapsed: false, railOpen: false },
  ])("the boot script restores exactly what the app saved: %o", (layout) => {
    applyShellLayout(layout);
    const saved = htmlState();
    document.documentElement.removeAttribute("style");
    delete document.documentElement.dataset.sidebar;
    delete document.documentElement.dataset.rail;

    runBootScript();
    expect(htmlState()).toEqual(saved);
    expect(readPersisted()).toEqual(layout);
  });

  it.each([
    ['{"sidebarWidth": 9999}', "360px"],
    ['{"sidebarWidth": 10}', "200px"],
    ["not json", ""],
    ['{"sidebarWidth": "wide"}', "240px"],
  ])("the boot script survives stored value %s", (stored, width) => {
    localStorage.setItem(SHELL_STORAGE_KEY, stored);
    runBootScript();
    expect(htmlState().width).toBe(width);
  });

  it("clamps widths to the allowed range", () => {
    expect(clampSidebarWidth(50)).toBe(200);
    expect(clampSidebarWidth(1000)).toBe(360);
    expect(clampSidebarWidth(251.6)).toBe(252);
  });
});
