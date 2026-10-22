import { describe, expect, it } from "vitest";

import { createFuzzyMatcher, type WorkerRequest, type WorkerResponse } from "./fuzzy-client";
import { matchItems } from "./match";

const ITEMS = Array.from({ length: 200 }, (_, index) => ({
  id: `repo-${String(index)}`,
  title: `service-${String(index).padStart(3, "0")}`,
  subtitle: `acme-gh/service-${String(index)}`,
}));

describe("the fuzzy worker (real, as bundled by Vite)", () => {
  it("answers with exactly what the main thread computes", async () => {
    const worker = new Worker(new URL("./fuzzy.worker.ts", import.meta.url), { type: "module" });
    const answer = new Promise<WorkerResponse>((resolve, reject) => {
      worker.onmessage = (event: MessageEvent<WorkerResponse>) => {
        resolve(event.data);
      };
      worker.onerror = (event) => {
        reject(new Error(event.message));
      };
    });
    worker.postMessage({ type: "load", set: "repos", items: ITEMS } satisfies WorkerRequest);
    worker.postMessage({
      type: "match",
      id: 7,
      set: "repos",
      query: "svc 12",
    } satisfies WorkerRequest);
    const response = await answer;
    worker.terminate();
    expect(response.id).toBe(7);
    expect(response.matches).toEqual(matchItems("svc 12", ITEMS));
    expect(response.matches.length).toBeGreaterThan(0);
  });

  it("is what the palette gets where workers exist", async () => {
    const matcher = createFuzzyMatcher();
    expect(matcher.mode()).toBe("worker");
    matcher.load("repos", ITEMS);
    expect(await matcher.match("repos", "service-19")).toEqual(matchItems("service-19", ITEMS));
    expect(matcher.mode()).toBe("worker");
    matcher.dispose();
  });
});
