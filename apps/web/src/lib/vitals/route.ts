/**
 * A page's route as a pattern (10.11): `/acme/api/code/backend/app/main.py` is
 * `/[org]/[repo]/code/[...path]`. What a web vital is stored with, so no repository's, file's
 * or person's name leaves the page with it: a segment is a param's name, one of the app's own
 * route words, or `_`.
 */

type Params = Record<string, string | string[] | undefined>;

/** The static segments of the app's routes under `/[org]` (route.test.ts checks the list
 * against the app directory). Anything else in a path is somebody's name. */
export const ROUTE_WORDS: ReadonlySet<string> = new Set([
  "activity", "api-keys", "ask", "changes", "code", "danger", "diagrams", "fixes", "general",
  "graph", "impact", "inbox", "index", "insights", "integrations", "map", "mcp", "members",
  "models", "new", "repos", "runs", "search", "security", "settings", "trace", "usage", "wiki",
]); // prettier-ignore

function decoded(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

export function routePattern(pathname: string, params: Params): string {
  const segments = pathname.split("/").filter(Boolean).map(decoded);
  // Next gives params as they are in the URL (percent-encoded): compared decoded, like the path.
  const named = Object.entries(params).flatMap(([name, value]): [string, string | string[]][] =>
    value === undefined ? [] : [[name, Array.isArray(value) ? value.map(decoded) : decoded(value)]],
  );
  const out: string[] = [];
  for (let i = 0; i < segments.length;) {
    const here = segments[i] ?? "";
    // A catch-all takes as many segments as it holds; a single param, one.
    const many = named.find(
      ([, value]) =>
        Array.isArray(value) &&
        value.length > 0 &&
        value.every((part, k) => segments[i + k] === part),
    );
    if (many && Array.isArray(many[1])) {
      out.push(`[...${many[0]}]`);
      i += many[1].length;
      continue;
    }
    const one = named.find(([, value]) => value === here);
    out.push(one ? `[${one[0]}]` : ROUTE_WORDS.has(here) ? here : "_");
    i += 1;
  }
  return `/${out.join("/")}`.slice(0, 200);
}
