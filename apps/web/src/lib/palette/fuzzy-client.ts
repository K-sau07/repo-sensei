/**
 * Talks to the fuzzy worker: sets of items are loaded once, then each query is matched there.
 * Where there is no worker (the server render, plain Node tests) or it fails to start, the same
 * matcher runs synchronously instead, so callers never need to know which one they got.
 */
import { matchItems, type ItemMatch } from "./match";
import type { Matchable } from "./types";

export type WorkerRequest =
  | { type: "load"; set: string; items: readonly Matchable[] }
  | { type: "match"; id: number; set: string; query: string };

export interface WorkerResponse {
  id: number;
  matches: ItemMatch[];
}

export interface FuzzyMatcher {
  readonly mode: () => "worker" | "sync";
  load: (set: string, items: readonly Matchable[]) => void;
  match: (set: string, query: string) => Promise<ItemMatch[]>;
  dispose: () => void;
}

/** A worker that hasn't answered by then (still starting, or stuck) is answered for here. */
const ANSWER_TIMEOUT_MS = 250;

export function createSyncMatcher(): FuzzyMatcher {
  const sets = new Map<string, readonly Matchable[]>();
  return {
    mode: () => "sync",
    load: (set, items) => {
      sets.set(set, items);
    },
    match: (set, query) => Promise.resolve(matchItems(query, sets.get(set) ?? [])),
    dispose: () => {
      sets.clear();
    },
  };
}

/** Over an existing worker (tests pass their own). Falls back to matching here for good if the
 * worker errors. */
export function createWorkerMatcher(worker: Worker): FuzzyMatcher {
  // Kept here too, so a failing worker can be replaced without losing what it was given.
  const sets = new Map<string, readonly Matchable[]>();
  const pending = new Map<number, { resolve: (matches: ItemMatch[]) => void; local: () => void }>();
  let next = 1;
  let broken = false;

  const fallBack = () => {
    broken = true;
    worker.terminate();
    for (const request of [...pending.values()]) request.local();
  };
  worker.onmessage = (event: MessageEvent<WorkerResponse>) => {
    const request = pending.get(event.data.id);
    if (!request) return;
    pending.delete(event.data.id);
    request.resolve(event.data.matches);
  };
  worker.onerror = fallBack;
  worker.onmessageerror = fallBack;

  return {
    mode: () => (broken ? "sync" : "worker"),
    load: (set, items) => {
      sets.set(set, items);
      if (!broken) worker.postMessage({ type: "load", set, items } satisfies WorkerRequest);
    },
    match: (set, query) => {
      if (broken) return Promise.resolve(matchItems(query, sets.get(set) ?? []));
      const id = next;
      next += 1;
      return new Promise((resolve) => {
        const timer = setTimeout(() => {
          pending.get(id)?.local();
        }, ANSWER_TIMEOUT_MS);
        const settle = (matches: ItemMatch[]) => {
          clearTimeout(timer);
          pending.delete(id);
          resolve(matches);
        };
        pending.set(id, {
          resolve: settle,
          local: () => {
            settle(matchItems(query, sets.get(set) ?? []));
          },
        });
        worker.postMessage({ type: "match", id, set, query } satisfies WorkerRequest);
      });
    },
    dispose: () => {
      worker.terminate();
      for (const request of [...pending.values()]) request.local();
      sets.clear();
    },
  };
}

/** A worker if the platform has one, otherwise the synchronous matcher. */
export function createFuzzyMatcher(): FuzzyMatcher {
  if (typeof window === "undefined" || typeof Worker === "undefined") return createSyncMatcher();
  try {
    // The `new URL(…, import.meta.url)` form is what Next (Turbopack) and Vite (Storybook,
    // Vitest's browser mode) both bundle as a worker.
    const worker = new Worker(new URL("./fuzzy.worker.ts", import.meta.url), {
      type: "module",
      name: "palette-fuzzy",
    });
    return createWorkerMatcher(worker);
  } catch {
    return createSyncMatcher();
  }
}

let shared: FuzzyMatcher | null = null;

/** One worker for the page, started the first time the palette needs it. */
export function sharedFuzzyMatcher(): FuzzyMatcher {
  shared ??= createFuzzyMatcher();
  return shared;
}
