import { describe, expect, it } from "vitest";

import {
  byLane,
  candidatesOf,
  countLabel,
  hopLocation,
  innermostAt,
  outlineRows,
  provenanceTag,
  targetOf,
  type GraphHop,
  type GraphNode,
} from "./graph";

function node(key: string, overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    symbol_key: key,
    kind: "function",
    name: key,
    qualified_name: key,
    path: "app/main.py",
    start_line: 1,
    end_line: 2,
    lang: "python",
    lane: "svc",
    ...overrides,
  };
}

function hop(
  key: string,
  edge: Partial<GraphHop["edge"]> = {},
  overrides: Partial<GraphNode> = {},
): GraphHop {
  return {
    node: node(key, overrides),
    edge: {
      type: "calls",
      confidence: 0.9,
      provenance: "import_resolved",
      evidence: null,
      direction: "out",
      ...edge,
    },
    depth: 1,
    via: "root",
  };
}

const symbol = (key: string, start_line: number, end_line: number) => ({
  symbol_key: key,
  name: key,
  kind: "function",
  start_line,
  end_line,
});

describe("provenanceTag", () => {
  it("is precise for what the code itself says, inferred for a model's", () => {
    expect(provenanceTag("scip")).toBe("precise");
    expect(provenanceTag("runtime")).toBe("precise");
    expect(provenanceTag("contract")).toBe("precise");
    expect(provenanceTag("import_resolved")).toBe("precise");
    expect(provenanceTag("receiver_heuristic")).toBe("heuristic");
    expect(provenanceTag("url_literal")).toBe("heuristic");
    expect(provenanceTag("llm_inferred")).toBe("inferred");
  });
});

describe("targetOf", () => {
  it("takes the hop named by the word, the one on that line first", () => {
    const hops = [
      hop("a", { evidence: { path: "app/main.py", line: 3 }, confidence: 1 }, { name: "save" }),
      hop("b", { evidence: { path: "app/main.py", line: 7 }, confidence: 0.5 }, { name: "save" }),
    ];
    expect(targetOf(hops, "save", 7, "app/main.py")?.node.symbol_key).toBe("b");
    expect(targetOf(hops, "save", 3, "app/main.py")?.node.symbol_key).toBe("a");
  });

  it("prefers a definition over an external, and reads a qualified name's last part", () => {
    const hops = [
      hop("ext", { confidence: 1 }, { kind: "external_symbol", path: null, name: "x" }),
      hop("def", { confidence: 0.7 }, { qualified_name: "sqlmodel/Session.exec", name: "x" }),
    ];
    expect(targetOf(hops, "x", 1, "app/main.py")?.node.symbol_key).toBe("def");
    expect(targetOf(hops, "exec", 1, "app/main.py")?.node.symbol_key).toBe("def");
  });

  it("only follows edges out of the definition, one hop away", () => {
    const hops = [
      hop("in", { direction: "in" }, { name: "w" }),
      { ...hop("far", {}, { name: "w" }), depth: 2 },
    ];
    expect(targetOf(hops, "w", 1, "app/main.py")).toBeNull();
  });
});

describe("candidatesOf", () => {
  it("is every node named on that line when the graph can't tell which, surest first", () => {
    const line = { path: "app/main.py", line: 4 };
    const hops = [
      hop("crud.save", { evidence: line, confidence: 0.5 }, { name: "save" }),
      hop("store.save", { evidence: line, confidence: 0.6 }, { name: "save" }),
      hop("store.save", { evidence: { ...line, line: 9 }, confidence: 0.9 }, { name: "save" }),
      hop("other.save", { evidence: { ...line, line: 9 }, confidence: 1 }, { name: "save" }),
    ];
    expect(candidatesOf(hops, "save", 4, "app/main.py").map((h) => h.node.symbol_key)).toEqual([
      "store.save",
      "crud.save",
    ]);
    // Nothing on the line: the likeliest elsewhere, alone.
    expect(candidatesOf(hops, "save", 5, "app/main.py").map((h) => h.node.symbol_key)).toEqual([
      "other.save",
    ]);
    expect(candidatesOf(hops, "load", 4, "app/main.py")).toEqual([]);
  });
});

describe("outlineRows and innermostAt", () => {
  const symbols = [
    symbol("Settings", 10, 40),
    symbol("Settings.url", 12, 15),
    symbol("Settings.emails", 20, 30),
    symbol("helper", 45, 50),
  ];

  it("nest each symbol under the ones holding it", () => {
    expect(outlineRows(symbols).map((row) => [row.symbol.symbol_key, row.depth])).toEqual([
      ["Settings", 0],
      ["Settings.url", 1],
      ["Settings.emails", 1],
      ["helper", 0],
    ]);
  });

  it("find the innermost definition around a line", () => {
    expect(innermostAt(symbols, 25)?.symbol_key).toBe("Settings.emails");
    expect(innermostAt(symbols, 18)?.symbol_key).toBe("Settings");
    expect(innermostAt(symbols, 42)).toBeNull();
  });
});

describe("byLane", () => {
  it("groups by lane in the swimlanes' order, surest first, empty lanes left out", () => {
    const groups = byLane([
      hop("test", { confidence: 0.9 }, { lane: "other" }),
      hop("route-b", { confidence: 0.7 }, { lane: "api", path: "b.py" }),
      hop("model", { confidence: 0.9 }, { lane: "db" }),
      hop("route-a", { confidence: 0.7 }, { lane: "api", path: "a.py" }),
      hop("route-sure", { confidence: 1 }, { lane: "api", path: "z.py" }),
    ]);
    expect(groups.map((g) => [g.lane, g.hops.map((h) => h.node.symbol_key)])).toEqual([
      ["api", ["route-sure", "route-a", "route-b"]],
      ["db", ["model"]],
      ["other", ["test"]],
    ]);
  });
});

describe("hopLocation", () => {
  it("is the evidence's line, else the node's start, else nowhere", () => {
    expect(hopLocation(hop("a", { evidence: { path: "x.ts", line: 9 } }))).toEqual({
      path: "x.ts",
      line: 9,
    });
    expect(hopLocation(hop("b", { evidence: { path: "x.ts", line: null } }))).toEqual({
      path: "x.ts",
      line: 1,
    });
    expect(hopLocation(hop("c", {}, { start_line: 4 }))).toEqual({ path: "app/main.py", line: 4 });
    expect(hopLocation(hop("d", {}, { path: null }))).toBeNull();
  });
});

describe("countLabel", () => {
  it("says when the limit cut it", () => {
    expect(countLabel(12, false)).toBe("12");
    expect(countLabel(1500, true)).toBe("1,500+");
  });
});
