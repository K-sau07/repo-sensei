import { describe, expect, it } from "vitest";

import {
  cycleEdges,
  edgeKey,
  expand,
  fanIn,
  finer,
  matching,
  neighborhood,
  nodeOfPath,
  shortestPath,
  sizeOf,
  tintOf,
  typesLabel,
  MAX_SIZE,
  MIN_SIZE,
  OPEN_RADIUS,
  type DepEdge,
  type DepNode,
  type Deps,
} from "./deps";

function node(id: string, x: number, y: number, cluster: string | null = null): DepNode {
  return { id, label: id.split("/").at(-1) ?? id, layer: "service", size: 1, cluster, x, y };
}

function edge(
  source: string,
  target: string,
  weight = 1,
  types: Record<string, number> = { calls: weight },
): DepEdge {
  return { source, target, weight, types, confidence: 0.9, samples: [] };
}

function level(at: Deps["level"], nodes: DepNode[], edges: DepEdge[]): Deps {
  return {
    level: at,
    sha: "abc",
    layout: "force",
    total: nodes.length,
    truncated: false,
    nodes,
    edges,
    clusters: [],
  };
}

const MODULES = level(
  "module",
  [node("app/api", -0.5, 0), node("app/core", 0.5, 0), node("app/db", 0, 0.5)],
  [edge("app/api", "app/core", 3), edge("app/core", "app/db", 2), edge("app/api", "app/db", 1)],
);
const FILES = level(
  "file",
  [
    node("app/api/items.py", 0.9, 0.9, "app/api"),
    node("app/api/users.py", 0.7, 0.9, "app/api"),
    node("app/core/auth.py", -1, -1, "app/core"),
    node("app/db/models.py", 0, 0, "app/db"),
  ],
  [
    edge("app/api/items.py", "app/core/auth.py", 2),
    edge("app/api/users.py", "app/core/auth.py", 1),
    edge("app/api/items.py", "app/api/users.py", 4),
    edge("app/core/auth.py", "app/db/models.py", 2),
    edge("app/api/users.py", "app/db/models.py", 1),
  ],
);

describe("the dependency graph", () => {
  it("names the level below and the color of a layer", () => {
    expect(finer("service")).toBe("package");
    expect(finer("file")).toBe("symbol");
    expect(finer("symbol")).toBeNull();
    expect(tintOf("handler")).toBe("api");
    expect(tintOf("state")).toBe("ui");
    expect(tintOf("something new")).toBe("other");
  });

  it("sizes a node by what depends on it", () => {
    const fan = fanIn(MODULES.edges);
    expect(fan.get("app/db")).toBe(3);
    expect(fan.get("app/api")).toBeUndefined();
    expect(sizeOf(0, 3)).toBe(MIN_SIZE);
    expect(sizeOf(3, 3)).toBe(MAX_SIZE);
    expect(sizeOf(1, 4)).toBeCloseTo(MIN_SIZE + (MAX_SIZE - MIN_SIZE) / 2);
    expect(sizeOf(5, 0)).toBe(MIN_SIZE);
  });

  it("finds a node's neighbours a number of hops out, either way", () => {
    const chain = [edge("a", "b"), edge("b", "c"), edge("d", "c"), edge("c", "e")];
    expect(neighborhood(chain, "b", 1)).toEqual(new Set(["a", "b", "c"]));
    expect(neighborhood(chain, "b", 2)).toEqual(new Set(["a", "b", "c", "d", "e"]));
    expect(neighborhood(chain, "z", 3)).toEqual(new Set(["z"]));
  });

  it("finds the fewest hops from one node to another, along what depends on what", () => {
    const edges = [edge("a", "b"), edge("b", "c"), edge("c", "d"), edge("a", "c")];
    expect(shortestPath(edges, "a", "d")).toEqual(["a", "c", "d"]);
    expect(shortestPath(edges, "d", "a")).toBeNull(); // not against the arrows
    expect(shortestPath(edges, "b", "b")).toEqual(["b"]);
  });

  it("places a search hit's file at each level", () => {
    const ids = new Set([".", "backend", "backend/app", "backend/app/api", "~Tables"]);
    expect(nodeOfPath(ids, "module", "backend/app/api/items.py")).toBe("backend/app/api");
    expect(nodeOfPath(ids, "module", "frontend/x.ts")).toBeNull();
    expect(nodeOfPath(ids, "package", "backend/app/api/items.py")).toBe("backend/app/api");
    expect(nodeOfPath(new Set([".", "backend"]), "service", "backend/a.py")).toBe("backend");
    expect(nodeOfPath(new Set([".", "backend"]), "service", "docs/a.md")).toBe(".");
    expect(nodeOfPath(new Set(["a/b.py"]), "file", "a/b.py")).toBe("a/b.py");
    expect(nodeOfPath(new Set(["py a/b.py/f()."]), "symbol", "a/b.py", "py a/b.py/f().")).toBe(
      "py a/b.py/f().",
    );
    expect(nodeOfPath(new Set(["py a/b.py/f()."]), "symbol", "a/b.py")).toBeNull();
  });

  it("finds nodes by name", () => {
    expect(matching(MODULES.nodes, " CORE ")).toEqual(["app/core"]);
    expect(matching(MODULES.nodes, "app/")).toHaveLength(3);
    expect(matching(MODULES.nodes, "  ")).toEqual([]);
  });

  it("opens a node into its children in place, edges going to what is shown", () => {
    const { nodes, edges } = expand(MODULES, FILES, new Set(["app/api"]));
    expect(nodes.map((n) => `${n.level}:${n.id}`).sort()).toEqual([
      "file:app/api/items.py",
      "file:app/api/users.py",
      "module:app/core",
      "module:app/db",
    ]);
    // Spread around where the module was, as the files' own layout places them.
    const items = nodes.find((n) => n.id === "app/api/items.py");
    const users = nodes.find((n) => n.id === "app/api/users.py");
    expect(items?.x).toBeCloseTo(-0.5 + OPEN_RADIUS);
    expect(users?.x).toBeCloseTo(-0.5 - OPEN_RADIUS);
    expect(items?.y).toBeCloseTo(0);
    const byKey = new Map(edges.map((e) => [edgeKey(e.source, e.target), e]));
    expect([...byKey.keys()].sort()).toEqual(
      [
        edgeKey("app/api/items.py", "app/api/users.py"), // now inside, shown
        edgeKey("app/api/items.py", "app/core"),
        edgeKey("app/api/users.py", "app/core"),
        edgeKey("app/api/users.py", "app/db"),
        edgeKey("app/core", "app/db"), // neither opened: the module level's own
      ].sort(),
    );
    expect(byKey.get(edgeKey("app/core", "app/db"))?.weight).toBe(2);
  });

  it("sums the edges that land on one pair once opened", () => {
    const coarse = level("module", [node("a", 0, 0), node("b", 1, 0)], [edge("a", "b", 3)]);
    const fine = level(
      "file",
      [node("a/x", 0, 0, "a"), node("b/y", 1, 0, "b"), node("b/z", 1, 1, "b")],
      [edge("a/x", "b/y", 1, { calls: 1 }), edge("a/x", "b/z", 2, { imports: 2 })],
    );
    const { nodes, edges } = expand(coarse, fine, new Set(["a"]));
    expect(nodes.map((n) => n.id)).toEqual(["a/x", "b"]);
    expect(nodes[0]).toMatchObject({ x: 0, y: 0 }); // an only child stays where its parent was
    expect(edges).toEqual([
      expect.objectContaining({
        source: "a/x",
        target: "b",
        weight: 3,
        types: { calls: 1, imports: 2 },
      }),
    ]);
  });

  it("leaves a node with no children below as it is, with its edges", () => {
    const fine = level(
      "file",
      FILES.nodes.filter((n) => n.cluster !== "app/db"),
      [],
    );
    const { nodes, edges } = expand(MODULES, fine, new Set(["app/db"]));
    expect(nodes.map((n) => `${n.level}:${n.id}`)).toContain("module:app/db");
    expect(edges.filter((e) => e.target === "app/db")).toHaveLength(2);
  });

  it("is the level itself with nothing opened or no level below", () => {
    expect(expand(MODULES, FILES, new Set()).edges).toBe(MODULES.edges);
    expect(expand(MODULES, null, new Set(["app/api"])).nodes).toHaveLength(3);
  });

  it("marks the edges that close a cycle, and says what an edge counts", () => {
    const found = cycleEdges([
      {
        nodes: ["a", "b"],
        edges: [
          { source: "a", target: "b", weight: 1 },
          { source: "b", target: "a", weight: 2 },
        ],
      },
    ]);
    expect(found).toEqual(new Set([edgeKey("a", "b"), edgeKey("b", "a")]));
    expect(typesLabel({ imports: 12, calls: 182, uses_hook: 12 })).toBe(
      "182 calls, 12 imports, 12 uses hook",
    );
  });
});
