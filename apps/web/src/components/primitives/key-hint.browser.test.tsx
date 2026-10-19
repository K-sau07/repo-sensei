import { describe, expect, it } from "vitest";
import { render } from "vitest-browser-react";

import { KeyHint } from "./key-hint";

describe("KeyHint", () => {
  it("prints platform symbols and reads naturally", async () => {
    const screen = await render(<KeyHint shortcut="shift+mod+p" platform="apple" />);
    const hint = screen.getByRole("img", { name: "Shift Command P" });
    await expect.element(hint).toHaveTextContent("⇧⌘P");
  });

  it("uses words on other platforms", async () => {
    const screen = await render(<KeyHint shortcut="mod+k" platform="other" />);
    await expect.element(screen.getByRole("img", { name: "Control K" })).toHaveTextContent("CtrlK");
  });

  it("separates sequence steps", async () => {
    const screen = await render(<KeyHint shortcut="g r" platform="other" />);
    await expect.element(screen.getByRole("img", { name: "G then R" })).toHaveTextContent("GthenR");
  });

  it("detects the platform from the browser when not given", async () => {
    const screen = await render(<KeyHint shortcut="mod+k" />);
    const expected = /Mac|iPhone|iPad/.test(navigator.platform) ? "Command K" : "Control K";
    await expect.element(screen.getByRole("img", { name: expected })).toBeVisible();
  });
});
