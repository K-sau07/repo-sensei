/**
 * A push, played in the mock API (5.6), from the browser:
 *   localStorage.setItem("sensei.mock.push", "1")
 * A push to the default branch changed `backend/app/models.py`, and its incremental run has
 * indexed it: the org stream says so (`repo_updated`) soon after a page connects, and the
 * staleness check compares what was cited with that commit (the file cited from it has changed;
 * the rest is as it was). The browse endpoints still read the commit before (mock repositories
 * keep one snapshot).
 */
export const PUSH_KEY = "sensei.mock.push";
export const PUSHED_SHA = "c0ffee1e5d0b6a0c8c1b7d3f2e9a4b5c6d7e8f90";
export const PUSHED_FILES = ["backend/app/models.py"] as const;
/** How long after a page connects to the org stream the pushed index lands. */
export const PUSH_DELAY_MS = 800;

export function mockPushed(): boolean {
  return typeof localStorage !== "undefined" && localStorage.getItem(PUSH_KEY) === "1";
}

/** What the staleness check finds for a citation of `path` at `sha`, the index now being the
 * mock snapshot's (`indexed`), or the pushed one's. */
export function staleReason(
  sha: string,
  path: string,
  indexed: string,
  known: (path: string) => boolean,
): "unchanged" | "changed" | "deleted" | "unknown" {
  const atIndexed = indexed.startsWith(sha);
  const atPushed = mockPushed() && PUSHED_SHA.startsWith(sha);
  if ((!atIndexed && !atPushed) || !known(path)) return "unknown"; // no such commit or file
  const changed = (PUSHED_FILES as readonly string[]).includes(path);
  return mockPushed() && atIndexed && changed ? "changed" : "unchanged";
}
