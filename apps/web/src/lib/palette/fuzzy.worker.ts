/**
 * Fuzzy matching off the main thread, for the palette's larger local sets (repositories,
 * history). It holds each set it's sent and answers queries against it with the same matcher
 * the main thread uses (see `fuzzy-client.ts`).
 */
import type { WorkerRequest, WorkerResponse } from "./fuzzy-client";
import { matchItems } from "./match";
import type { Matchable } from "./types";

// The app's TypeScript lib is the DOM's (the worker lib conflicts with it), so the worker's
// global is described by what it uses.
interface WorkerScope {
  onmessage: ((event: MessageEvent<WorkerRequest>) => void) | null;
  postMessage: (message: WorkerResponse) => void;
}

const scope = self as unknown as WorkerScope;
const sets = new Map<string, readonly Matchable[]>();

scope.onmessage = (event) => {
  const request = event.data;
  if (request.type === "load") {
    sets.set(request.set, request.items);
    return;
  }
  scope.postMessage({
    id: request.id,
    matches: matchItems(request.query, sets.get(request.set) ?? []),
  });
};
