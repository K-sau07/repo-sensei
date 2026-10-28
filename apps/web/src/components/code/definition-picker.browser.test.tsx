import { describe, expect, it, vi } from "vitest";
import { render } from "vitest-browser-react";

import type { GraphHop } from "@/lib/api/graph";

import { DefinitionPicker } from "./definition-picker";

function hop(key: string, path: string | null, confidence: number): GraphHop {
  return {
    node: {
      symbol_key: key,
      kind: path ? "function" : "external_symbol",
      name: "save",
      qualified_name: path ? `${path.replace(/\.py$/, "")}.save` : "sqlmodel/Session.save",
      path,
      start_line: 12,
      end_line: 20,
      lang: "python",
      lane: path ? "svc" : "other",
    },
    edge: {
      type: "calls",
      confidence,
      provenance: "name_ambiguous",
      evidence: { path: "app/routes.py", line: 4 },
      direction: "out",
    },
    depth: 1,
    via: "py app/routes.py/create().",
  };
}

describe("the definition picker", () => {
  it("lists each candidate with where it is and how sure, and opens the one picked", async () => {
    const hops = [hop("a", "app/crud.py", 0.5), hop("b", "app/store.py", 0.5), hop("x", null, 0.3)];
    const candidates = hops.map((h) => ({ symbol: { ...h.node, definedHere: false }, hop: h }));
    const onPick = vi.fn();
    const screen = await render(
      <DefinitionPicker
        choice={{ word: "save", candidates }}
        onPick={onPick}
        onClose={() => undefined}
      />,
    );
    await expect.element(screen.getByRole("dialog", { name: "Where save goes" })).toBeVisible();
    await expect
      .element(screen.getByText("The graph has 3 definitions it could mean.", { exact: false }))
      .toBeVisible();
    await expect
      .element(screen.getByRole("button", { name: /app\/crud\.save.*app\/crud\.py:12/ }))
      .toBeVisible();
    // A library's is listed but can't be opened.
    await expect
      .element(screen.getByRole("button", { name: /Outside the repository/ }))
      .toBeDisabled();

    // A DOM click: the locator's click never finds the dialog's popup stable here.
    (screen.getByRole("button", { name: /app\/store\.save/ }).element() as HTMLElement).click();
    expect(onPick).toHaveBeenCalledWith(
      expect.objectContaining({ symbol_key: "b", path: "app/store.py" }),
    );
  });
});
