/**
 * A page's route as a pattern (10.11): `/acme/api/code/backend/app/main.py` is
 * `/[org]/[repo]/code/[...path]`. What a web vital is stored with, so no repository's,
 * file's or person's name leaves the page with it.
 */

type Params = Record<string, string | string[] | undefined>;

const SAFE = /^[\w.-]+$/; // what the API takes in a route's segment (besides the brackets)

function decoded(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

export function routePattern(pathname: string, params: Params): string {
  const segments = pathname.split("/").filter(Boolean).map(decoded);
  const named = Object.entries(params).filter(
    (entry): entry is [string, string | string[]] => entry[1] !== undefined,
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
    // Anything else is the route's own words; one that isn't is no name of ours to send.
    out.push(one ? `[${one[0]}]` : SAFE.test(here) ? here : "_");
    i += 1;
  }
  return `/${out.join("/")}`.slice(0, 200);
}
