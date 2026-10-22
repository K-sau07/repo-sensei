import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "vitest-browser-react";

import { useListState } from "./palette-input";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useListState", () => {
  it("keeps one observer however often the palette re-renders (each keystroke does)", async () => {
    const Real = MutationObserver;
    let created = 0;
    vi.stubGlobal(
      "MutationObserver",
      class extends Real {
        constructor(callback: MutationCallback) {
          super(callback);
          created += 1;
        }
      },
    );
    const list = document.createElement("div");
    list.id = "results";
    list.innerHTML = '<div cmdk-item id="a" aria-selected="false"></div>';
    document.body.append(list);
    const ref = { current: list };

    const { result, rerender } = await renderHook(() => useListState(ref));
    for (let keystroke = 0; keystroke < 10; keystroke += 1) await rerender();
    expect(created).toBe(1);
    expect(result.current).toEqual({ listId: "results", activeId: undefined });

    // It follows the selection as the list changes.
    list.querySelector("#a")?.setAttribute("aria-selected", "true");
    await vi.waitFor(() => {
      expect(result.current.activeId).toBe("a");
    });
    list.remove();
  });
});
