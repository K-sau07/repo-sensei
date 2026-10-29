import { describe, expect, it } from "vitest";

import {
  breaksByHop,
  definitionPlace,
  downHops,
  edgeLabel,
  hopName,
  hopPlace,
  howReached,
  isAmbiguous,
  isReturn,
  outcomeLabel,
  returnGroups,
  pathFiles,
  pathOutcome,
  pathTitle,
  pipOf,
  reachedAt,
  resolveQuestion,
  type TraceBreak,
  type TraceEdge,
  type TraceHop,
  type TraceNode,
  type TraceOutcome,
  type TracePath,
} from "./trace";

function node(name: string, overrides: Partial<TraceNode> = {}): TraceNode {
  return {
    kind: "function",
    name,
    qualified_name: name,
    symbol_key: `py a.py/${name}().`,
    path: "a.py",
    lines: [10, 20],
    lane: "svc",
    model: null,
    ...overrides,
  };
}

function edge(overrides: Partial<TraceEdge> = {}): TraceEdge {
  return {
    type: "calls",
    confidence: 0.9,
    provenance: "import_resolved",
    evidence: { path: "a.py", line: 12 },
    alternatives: 1,
    ...overrides,
  };
}

const hop = (
  name: string,
  edgeOf: TraceEdge | null = edge(),
  overrides: Partial<TraceNode> = {},
): TraceHop => ({ layer: "service", node: node(name, overrides), edge: edgeOf });

function path(hops: TraceHop[], overrides: Partial<TracePath> = {}): TracePath {
  return {
    hops,
    path_confidence: 0.81,
    layers: ["service"],
    reaches_goal: true,
    breaks: [],
    entry_index: 0,
    reaches_origin: false,
    ...overrides,
  };
}

describe("pipOf and isAmbiguous", () => {
  it("is verified for what the code proves, inferred for a rule's, none for the entry", () => {
    expect(pipOf(edge({ provenance: "import_resolved" }))).toBe("verified");
    expect(pipOf(edge({ provenance: "contract" }))).toBe("verified");
    expect(pipOf(edge({ provenance: "url_literal" }))).toBe("inferred");
    expect(pipOf(edge({ provenance: "receiver_heuristic" }))).toBe("inferred");
    expect(pipOf(edge({ provenance: "llm_inferred" }))).toBe("inferred");
    expect(pipOf(null)).toBeNull();
  });

  it("calls a hop ambiguous when the one before had other edges of its type", () => {
    expect(isAmbiguous(edge({ alternatives: 2 }))).toBe(true);
    expect(isAmbiguous(edge())).toBe(false);
    expect(isAmbiguous(null)).toBe(false);
  });

  it("says how a hop is reached of the hop itself, an override step in its own words", () => {
    expect(edgeLabel(edge({ type: "implements" }))).toBe("implements it");
    expect(edgeLabel(edge({ type: "uses_hook" }))).toBe("used");
    expect(edgeLabel(edge({ type: "handled_by" }))).toBe("handles it");
    expect(edgeLabel(edge({ type: "reads" }))).toBe("read");
  });

  it("names a method with its owner and a request by its path", () => {
    const at = (overrides: Partial<TraceNode>) => hopName(node("login", overrides));
    expect(at({ kind: "method", qualified_name: "AuthService.login" })).toBe("AuthService.login");
    expect(at({ kind: "method", qualified_name: "app.crud.UserCrud.login" })).toBe(
      "UserCrud.login",
    );
    expect(at({ kind: "method", qualified_name: "login" })).toBe("login");
    expect(at({ kind: "function", qualified_name: "app.crud.login" })).toBe("login");
    const call = (name: string) => hopName(node(name, { kind: "http_client_call" }));
    expect(call("POST http://localhost:8080/auth/login")).toBe("POST /auth/login");
    expect(call("GET https://api.example.com")).toBe("GET /");
    expect(call("GET /api/v1/items/")).toBe("GET /api/v1/items/");
    expect(call("GET ${API_BASE}/items")).toBe("GET ${API_BASE}/items");
  });
});

describe("hopPlace and definitionPlace", () => {
  it("is the call site that proves the hop, else the entry's own lines", () => {
    expect(hopPlace(hop("save"))).toEqual({ path: "a.py", start: 12, end: 12 });
    expect(hopPlace(hop("save", edge({ evidence: { path: "b.py", line: null } })))).toEqual({
      path: "b.py",
      start: 1,
      end: 1,
    });
    expect(hopPlace(hop("main", null))).toEqual({ path: "a.py", start: 10, end: 20 });
    expect(hopPlace(hop("table", null, { path: null, lines: null }))).toBeNull();
  });

  it("puts a table at its ORM class", () => {
    const model = { symbol_key: "py m.py/User#", path: "m.py", lines: [5, 9] as [number, number] };
    expect(definitionPlace(node("user", { kind: "table", model }))).toEqual({
      path: "m.py",
      start: 5,
      end: 9,
    });
    expect(definitionPlace(node("save"))).toEqual({ path: "a.py", start: 10, end: 20 });
    expect(definitionPlace(node("x", { path: null }))).toBeNull();
  });
});

describe("a path in words", () => {
  const model = { symbol_key: "py m.py/User#", path: "m.py", lines: [5, 9] as [number, number] };
  const resolved = path([
    hop("Login", null, { path: "login.tsx" }),
    hop("useAuth", edge(), { path: "useAuth.ts" }),
    hop("login", edge(), { path: "useAuth.ts" }),
    hop("user", edge({ type: "reads" }), { kind: "table", path: "m.py", model }),
  ]);

  it("names its ends and what it reaches", () => {
    expect(pathTitle(resolved)).toBe("Login → user");
    expect(pathOutcome(resolved, null)).toBe("Reaches the user table");
    expect(pathOutcome(resolved, node("authenticate"))).toBe("Reaches authenticate");
    expect(pathTitle(path([hop("alone", null)]))).toBe("alone");
  });

  it("lists its files in order, each once", () => {
    expect(pathFiles(resolved)).toEqual(["login.tsx", "useAuth.ts", "m.py"]);
  });

  it("says where a broken one stops, and asks about the gap", () => {
    const gap: TraceBreak = {
      after_hop: 1,
      kind: "gap",
      reason: "client call has no matching endpoint",
      detail: "no POST endpoint at /x (it has GET)",
    };
    const broken = path(
      [
        hop("Export", null),
        hop("POST /x", edge({ type: "issues_request", evidence: { path: "e.tsx", line: 7 } })),
      ],
      { reaches_goal: false, breaks: [gap] },
    );
    expect(pathOutcome(broken, null)).toBe("Stops: client call has no matching endpoint");
    expect(breaksByHop(broken).get(1)).toBe(gap);
    expect(resolveQuestion(broken, gap)).toBe(
      "The trace from Export stops after POST /x (e.tsx:7): client call has no matching " +
        "endpoint (no POST endpoint at /x (it has GET)). Where does it go next, and what in the " +
        "code connects them?",
    );
  });

  it("says an end and the hop limit as what they are, not as gaps", () => {
    const stop = (kind: TraceBreak["kind"], reason: string) =>
      path([hop("a", null)], { reaches_goal: false, breaks: [{ after_hop: 0, kind, reason }] });
    expect(
      pathOutcome(stop("end", "AuthResponse calls no more of the repository's code"), null),
    ).toBe("Ends: AuthResponse calls no more of the repository's code");
    expect(pathOutcome(stop("limit", "the trace shows 8 hops; getSignKey goes on"), null)).toBe(
      "The trace shows 8 hops; getSignKey goes on",
    );
  });
});

describe("the return leg", () => {
  const outcome = (overrides: Partial<TraceOutcome>): TraceOutcome => ({
    kind: "storage",
    action: "localStorage.setItem",
    target: "token",
    value: "res.data.token",
    after_call: null,
    on: null,
    ...overrides,
  });
  const produced = (name: string, of: TraceOutcome, line: number): TraceHop => ({
    layer: "state",
    node: node(name, { kind: "client_outcome", outcome: of }),
    edge: edge({ type: "produces", evidence: { path: "login.tsx", line } }),
  });
  const token = outcome({});
  const home = outcome({ kind: "navigate", action: "navigate", target: "/dashboard" });
  const failed = outcome({ kind: "state", action: "setError", target: "setError", on: "error" });
  const returning = path(
    [
      hop("Login", null, { path: "login.tsx" }),
      hop("login", edge({ evidence: { path: "login.tsx", line: 12 } })),
      hop("user", edge({ type: "reads" }), { kind: "table", path: null, lines: null }),
      hop("Login", edge({ evidence: { path: "login.tsx", line: 12 } }), { path: "login.tsx" }),
      produced("localStorage.setItem token", token, 13),
      produced("navigate /dashboard", home, 14),
      produced("setError", failed, 17),
    ],
    { return_index: 3 },
  );

  it("comes after the way down, which names the path", () => {
    expect(downHops(returning).map((h) => h.node.name)).toEqual(["Login", "login", "user"]);
    expect(isReturn(returning, 2)).toBe(false);
    expect(isReturn(returning, 3)).toBe(true);
    expect(pathTitle(returning)).toBe("Login → user");
    expect(pathOutcome(returning, null)).toBe("Reaches the user table");
    expect(downHops(path([hop("a", null)]))).toHaveLength(1);
  });

  it("groups what each function does with the response, in order", () => {
    expect(
      returnGroups(returning).map((g) => [g.index, g.outcomes.map((step) => step.index)]),
    ).toEqual([[3, [4, 5, 6]]]);
    expect(returnGroups(path([hop("a", null)]))).toEqual([]);
  });

  it("says each step in words: back to the function, then what it does", () => {
    expect(howReached(returning, 3)).toBe("returns to");
    expect(howReached(returning, 4)).toBe("stores");
    expect(howReached(returning, 5)).toBe("navigates");
    expect(howReached(returning, 6)).toBe("on error, sets");
    expect(howReached(returning, 1)).toBe("called");
    expect(reachedAt(returning, 1)).toBe("called at");
    expect(reachedAt(returning, 3)).toBe("returns to");
    expect(outcomeLabel(token)).toBe("localStorage.setItem token");
    expect(outcomeLabel(home)).toBe("→ /dashboard");
    expect(outcomeLabel(failed)).toBe("setError");
  });
});
