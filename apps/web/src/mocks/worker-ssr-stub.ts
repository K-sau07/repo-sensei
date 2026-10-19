/**
 * Stand-in for `msw/browser` outside the browser. MSW publishes no server build of it, but Next
 * also compiles client components for server rendering, where the worker is never started.
 */
export function setupWorker(): never {
  throw new Error("The MSW browser worker can only run in the browser.");
}
