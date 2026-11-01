import { describe, expect, it } from "vitest";

import { elkGraph, layerColumn, layout, positionsOf } from "@/lib/diagrams/elk-layout";
import { exportSvg } from "@/lib/diagrams/export";

import {
  citationLabel,
  citedFiles,
  outline,
  redrawInput,
  shownEdges,
  specDiff,
  specOf,
  verifiedPercent,
  type Diagram,
  type Spec,
} from "./diagrams";

const [PAGE, ENDPOINT, SERVICE] = [
  {
    id: "a",
    label: "Login page",
    layer: "ui",
    kind: "component",
    citation: { path: "web/login.tsx", start_line: 3, end_line: 40 },
  },
  { id: "b", label: "POST /login", layer: "route", kind: "api_endpoint", citation: null },
  {
    id: "c",
    label: "authenticate",
    layer: "service",
    kind: "function",
    citation: { path: "api/crud.py", start_line: 12, end_line: 12 },
  },
] as const;

const SPEC: Spec = {
  title: "Login",
  kind: "flowchart",
  nodes: [PAGE, ENDPOINT, SERVICE],
  edges: [
    {
      from: "a",
      to: "b",
      type: "targets",
      label: "posts the form",
      verified: true,
      confidence: 0.9,
      provenance: "url_literal",
      inferred: null,
    },
    {
      from: "b",
      to: "c",
      type: null,
      label: null,
      verified: false,
      confidence: null,
      provenance: null,
      inferred: { reason: "a dependency injected at runtime" },
    },
  ],
};

describe("a diagram's spec", () => {
  it("is read as the API wrote it, with how much the graph proves", () => {
    expect(specOf({ spec: SPEC as unknown as Diagram["spec"] })).toEqual(SPEC);
    expect(specOf({ spec: {} }).nodes).toEqual([]);
    expect(verifiedPercent(0.835)).toBe("84%");
    expect(shownEdges(SPEC, true)).toHaveLength(1);
    expect(shownEdges(SPEC, false)).toHaveLength(2);
    expect(citationLabel(SPEC.nodes[0]?.citation ?? null)).toBe("login.tsx:3–40");
    expect(citationLabel(SPEC.nodes[2]?.citation ?? null)).toBe("crud.py:12");
    expect(citationLabel(null)).toBeNull();
  });

  it("names its cited files once, at its commit, and says itself as text", () => {
    expect(citedFiles(SPEC, "abc1234")).toEqual([
      { sha: "abc1234", path: "api/crud.py" },
      { sha: "abc1234", path: "web/login.tsx" },
    ]);
    expect(outline(SPEC)).toEqual([
      "Login page → POST /login: posts the form",
      "POST /login → authenticate (inferred: a dependency injected at runtime)",
    ]);
  });

  it("knows what a regenerated diagram added and dropped", () => {
    const after: Spec = {
      ...SPEC,
      nodes: [...SPEC.nodes.slice(1), { ...SERVICE, id: "d" }],
    };
    const diff = specDiff(SPEC, after);
    expect([...diff.added]).toEqual(["d"]);
    expect(diff.removed.map((n) => n.id)).toEqual(["a"]);
  });

  it("is drawn again from what it was drawn from", () => {
    const base = { kind: "sequence", prompt: {} } as unknown as Diagram;
    expect(redrawInput({ ...base, prompt: { question: "login?" } })).toEqual({
      question: "login?",
      kind: "sequence",
    });
    const fromTrace = { from: "x", direction: "down", kind: "auto", path: 1 } as const;
    expect(redrawInput({ ...base, prompt: { from_trace: fromTrace } })).toEqual({
      from_trace: fromTrace,
      kind: "sequence",
    });
    expect(redrawInput(base)).toBeNull();
  });
});

describe("its layout and exports", () => {
  it("is laid out by ELK, an architecture one in columns by layer", async () => {
    const graph = elkGraph(SPEC, SPEC.edges, "RIGHT");
    expect(graph.children?.map((c) => c.id)).toEqual(["a", "b", "c"]);
    expect(graph.edges).toHaveLength(2);
    const laid = positionsOf(await layout(graph));
    expect(Object.keys(laid)).toEqual(["a", "b", "c"]);
    expect(laid.a?.x ?? 0).toBeLessThan(laid.b?.x ?? 0); // left to right
    const columns = elkGraph({ ...SPEC, kind: "architecture" }, SPEC.edges, "RIGHT");
    expect(columns.children?.[0]?.layoutOptions?.["elk.partitioning.partition"]).toBe("0");
    expect(layerColumn("db")).toBe(6);
    expect(layerColumn("nonsense")).toBe(8);
  });

  it("exports an SVG with its labels escaped", () => {
    const svg = exportSvg(
      { ...SPEC, nodes: [{ ...PAGE, label: "<script>" }, ...SPEC.nodes.slice(1)] },
      SPEC.edges,
      { a: { x: 0, y: 0 }, b: { x: 300, y: 0 }, c: { x: 600, y: 0 } },
    );
    expect(svg.startsWith("<svg")).toBe(true);
    expect(svg).toContain("&lt;script&gt;");
    expect(svg).not.toContain("<script>");
    expect(svg).toContain('stroke-dasharray="6 4"'); // the inferred edge
  });
});
