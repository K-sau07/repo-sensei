/** OAuth sign-in for editors and the CLI (7.2): what a scope lets a client do, in words. */

export const SCOPE_LABELS: Record<string, { title: string; detail: string }> = {
  "read:repo": {
    title: "Read your repositories",
    detail:
      "Search, read code, trace flows and explore the dependency graph. Searching may use some of your organization's AI budget.",
  },
  ask: {
    title: "Ask questions",
    detail: "Get answers with citations, spending your organization's AI budget.",
  },
  "connect:repo": {
    title: "Connect repositories",
    detail:
      "Add repositories to your organization if you're an admin. Each one is indexed right away, which spends your organization's budget.",
  },
};

/** Scopes in the order they're shown: reading, then what goes further (unknown ones last). */
export function sortScopes(scopes: readonly string[]): string[] {
  const order = Object.keys(SCOPE_LABELS);
  const rank = (scope: string) => {
    const at = order.indexOf(scope);
    return at === -1 ? order.length : at;
  };
  return [...scopes].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

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
