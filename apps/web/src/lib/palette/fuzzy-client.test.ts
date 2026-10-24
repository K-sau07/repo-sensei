import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createFuzzyMatcher,
  createWorkerMatcher,
  type WorkerRequest,
  type WorkerResponse,
} from "./fuzzy-client";
import { matchItems } from "./match";

const ITEMS = [
  { id: "a", title: "api", subtitle: "acme-gh/api" },
  { id: "b", title: "web", subtitle: "acme-gh/web" },
  { id: "c", title: "infra", subtitle: "acme-gh/infra" },
];

/** A stand-in Worker that runs the worker's protocol in-process (or not at all). */
class FakeWorker {
  onmessage: ((event: MessageEvent<WorkerResponse>) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessageerror: (() => void) | null = null;
  terminated = false;
  sets = new Map<string, typeof ITEMS>();
  constructor(private readonly mode: "answer" | "silent" = "answer") {}
  postMessage(request: WorkerRequest) {
    if (request.type === "load") {
      this.sets.set(request.set, [...request.items] as typeof ITEMS);
      return;
    }
    if (this.mode === "silent") return;
    const matches = matchItems(request.query, this.sets.get(request.set) ?? []);
    queueMicrotask(() => {
      this.onmessage?.({ data: { id: request.id, matches } } as MessageEvent<WorkerResponse>);
    });
  }
  terminate() {
    this.terminated = true;
  }
}

afterEach(() => {
  vi.useRealTimers();
});

describe("fuzzy matcher", () => {
  it("falls back to matching synchronously where there is no Worker (Node, the server)", async () => {
    const matcher = createFuzzyMatcher();
    expect(matcher.mode()).toBe("sync");
    matcher.load("repos", ITEMS);
    expect(await matcher.match("repos", "inf")).toEqual(matchItems("inf", ITEMS));
  });

  it("answers from the worker with the same results the main thread would give", async () => {
    const worker = new FakeWorker();
    const matcher = createWorkerMatcher(worker as unknown as Worker);
    matcher.load("repos", ITEMS);
    expect(matcher.mode()).toBe("worker");
    expect(await matcher.match("repos", "a")).toEqual(matchItems("a", ITEMS));
    expect(await matcher.match("other", "a")).toEqual([]);
  });

  it("matches here if the worker errors, from then on", async () => {
    const worker = new FakeWorker("silent");
    const matcher = createWorkerMatcher(worker as unknown as Worker);
    matcher.load("repos", ITEMS);
    const pending = matcher.match("repos", "web");
    worker.onerror?.();
    expect(await pending).toEqual(matchItems("web", ITEMS));
    expect(worker.terminated).toBe(true);
    expect(matcher.mode()).toBe("sync");
    expect(await matcher.match("repos", "inf")).toEqual(matchItems("inf", ITEMS));
  });

  it("answers here for a worker that doesn't answer in time", async () => {
    vi.useFakeTimers();
    const matcher = createWorkerMatcher(new FakeWorker("silent") as unknown as Worker);
    matcher.load("repos", ITEMS);
    const pending = matcher.match("repos", "api");
    await vi.advanceTimersByTimeAsync(300);
    expect(await pending).toEqual(matchItems("api", ITEMS));
  });
});
