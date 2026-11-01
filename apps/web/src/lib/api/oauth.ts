/** OAuth sign-in for editors and the CLI (7.2): what a scope lets a client do, in words. */

export const SCOPE_LABELS: Record<string, { title: string; detail: string }> = {
  "read:repo": {
    title: "Read your repositories",
    detail: "Search, read code, trace flows and explore the dependency graph.",
  },
  ask: {
    title: "Ask questions",
    detail: "Get answers with citations, spending your organization's AI budget.",
  },
};

export function scopeLabel(scope: string): { title: string; detail: string } {
  return SCOPE_LABELS[scope] ?? { title: scope, detail: "" };
}

/** A user code as people type it ("bcdf ghjk") in the shown form ("BCDF-GHJK"). */
export function formatUserCode(typed: string): string {
  const letters = typed
    .toUpperCase()
    .replace(/[^A-Z]/g, "")
    .slice(0, 8);
  return letters.length > 4 ? `${letters.slice(0, 4)}-${letters.slice(4)}` : letters;
}
