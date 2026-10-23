import { describe, expect, it } from "vitest";
import { renderHook } from "vitest-browser-react";

import type { FuzzyMatcher } from "@/lib/palette/fuzzy-client";

import { useWorkerMatches } from "./use-worker-matches";

const ITEMS = [
  { id: "a", title: "acme/api" },
  { id: "b", title: "acme/web" },
];

/** A worker still starting: it never answers. */
const silent: FuzzyMatcher = {
  mode: () => "worker",
  load: () => undefined,
  match: () => new Promise(() => undefined),
  dispose: () => undefined,
};

describe("useWorkerMatches", () => {
  it("shows the opening rows that match the first keystroke before the worker answers", async () => {
    const { result, rerender } = await renderHook(
      (props?: { query: string }) => useWorkerMatches("repos", ITEMS, props?.query ?? "", silent),
      { initialProps: { query: "" } },
    );
    expect(result.current.map((match) => match.id)).toEqual(["a", "b"]);
    await rerender({ query: "web" });
    expect(result.current.map((match) => match.id)).toEqual(["b"]);
  });
});
