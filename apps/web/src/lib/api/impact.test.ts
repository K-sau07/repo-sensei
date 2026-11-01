import { describe, expect, it } from "vitest";

import {
  allAffected,
  blastLayout,
  heatColor,
  impactHref,
  percent,
  prNote,
  type Affected,
  type Impact,
} from "./impact";

const CHANGED = {
  key: "py app/crud.py/create_user().",
  name: "create_user",
  kind: "function",
  path: "app/crud.py",
  start_line: 10,
  end_line: 20,
};

function affected(key: string, distance: number, via: string, score: number): Affected {
  return {
    key,
    name: key.split("/").at(-1) ?? key,
    kind: "function",
    path: "app/x.py",
    start_line: distance,
    end_line: distance + 1,
    score,
    distance,
    via,
    edge: "calls",
  };
}

const ROUTE = {
  ...affected("api_endpoint POST /users", 2, "handler", 0.68),
  name: "POST /users",
  kind: "api_endpoint",
  path: null,
};
const HANDLER = affected("handler", 1, CHANGED.key, 0.81);
const TABLE = { ...affected("table user", 1, CHANGED.key, 0.85), kind: "table", path: null };

const IMPACT: Impact = {
  sha: "abcdef1234",
  changed: [CHANGED],
  affected: { routes: [ROUTE], screens: [], tests: [], tables: [TABLE], symbols: [HANDLER] },
  heat: {},
  truncated: false,
};

describe("impact", () => {
  it("links to the page for a symbol or for files", () => {
    expect(impactHref("acme", "api", { symbol: CHANGED.key })).toBe(
      "/acme/api/impact?symbol=py+app%2Fcrud.py%2Fcreate_user%28%29.",
    );
    expect(impactHref("acme", "api", { files: ["a.py", "b.py"] })).toBe(
      "/acme/api/impact?files=a.py&files=b.py",
    );
    expect(impactHref("acme", "api")).toBe("/acme/api/impact");
  });

  it("draws the change at the center and each hop on its own ring, a branch together", () => {
    const placed = blastLayout(IMPACT.changed, allAffected(IMPACT));
    expect(placed.get(CHANGED.key)).toEqual({ x: 0, y: 0 });
    const radius = (key: string) => {
      const at = placed.get(key);
      return at ? Math.hypot(at.x, at.y) : NaN;
    };
    expect(radius(HANDLER.key)).toBeCloseTo(0.475);
    expect(radius(TABLE.key)).toBeCloseTo(0.475);
    expect(radius(ROUTE.key)).toBeCloseTo(0.95);
    // The route, alone on its ring, sits beside the handler it is reached through.
    const [h, r] = [placed.get(HANDLER.key), placed.get(ROUTE.key)];
    expect(Math.atan2(r?.y ?? 0, r?.x ?? 0)).toBeCloseTo(Math.atan2(h?.y ?? 0, h?.x ?? 0));
  });

  it("colors by score from the signal color through warn to danger", () => {
    const ramp = { low: "#000000", mid: "#808080", high: "#ffffff" };
    expect(heatColor(0, ramp)).toBe("#000000");
    expect(heatColor(0.5, ramp)).toBe("#808080");
    expect(heatColor(1, ramp)).toBe("#ffffff");
    expect(heatColor(2, ramp)).toBe("#ffffff");
    expect(percent(0.678)).toBe("68%");
  });

  it("writes a PR note: by category, ranked, with how sure and how far", () => {
    const note = prNote(IMPACT);
    expect(note).toContain("### Impact of changing `create_user`");
    expect(note).toContain("**Routes** (1)\n\n- `POST /users` (68% sure, 2 hops)");
    expect(note).toContain("**Tables** (1)");
    expect(note).toContain("- `handler` — `app/x.py:1` (81% sure, 1 hop)");
    expect(note.indexOf("**Routes**")).toBeLessThan(note.indexOf("**Other code**"));
    expect(note).toContain("`abcdef1`");
    expect(prNote(IMPACT, { listed: 0 })).toContain("- …and 1 more");
    const none = prNote({
      ...IMPACT,
      affected: { routes: [], screens: [], tests: [], tables: [], symbols: [] },
    });
    expect(none).toContain("Nothing in the code graph depends on it.");
  });
});
