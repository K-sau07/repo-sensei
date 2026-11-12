import { describe, expect, it } from "vitest";

import { elkGraph, layout, positionsOf } from "@/lib/diagrams/elk-layout";
import { erGraph, relationEnds, tableHeight } from "@/lib/diagrams/er-layout";
import { exportErSvg, exportSvg, exportSystemSvg } from "@/lib/diagrams/export";
import { layoutSystem, rowOf } from "@/lib/diagrams/system-layout";
import { appArchitecture, appSchema } from "@/mocks/app-architecture";

import {
  citationLabel,
  citedFiles,
  outline,
  redrawInput,
  shownEdges,
  specDiff,
  specOf,
  verifiedPercent,
  viewsOf,
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
    expect(redrawInput({ ...base, prompt: { whole_schema: true } })).toEqual({
      whole_schema: true,
      kind: "er",
    });
    expect(redrawInput(base)).toBeNull();
  });
});

describe("its layout and exports", () => {
  it("is laid out by ELK", async () => {
    const graph = elkGraph(SPEC, SPEC.edges, "RIGHT");
    expect(graph.children?.map((c) => c.id)).toEqual(["a", "b", "c"]);
    expect(graph.edges).toHaveLength(2);
    const laid = positionsOf(await layout(graph));
    expect(Object.keys(laid)).toEqual(["a", "b", "c"]);
    expect(laid.a?.x ?? 0).toBeLessThan(laid.b?.x ?? 0); // left to right
  });

  it("draws an architecture in rows, top to bottom, as on a whiteboard", () => {
    const spec = appArchitecture("Architecture");
    const placed = layoutSystem(spec, spec.edges);
    expect(placed.rows.map((r) => r.row.row)).toEqual([
      "actor",
      "frontend",
      "api",
      "service",
      "data",
      "outside",
    ]);
    const y = (id: string) => placed.at[id]?.y ?? -1;
    expect(y("actor:user")).toBeLessThan(y("part:web-app"));
    expect(y("part:web-app")).toBeLessThan(y("part:rest-api"));
    expect(y("part:data-models")).toBeLessThan(y("outside:postgresql"));
    // Each row centered, its boxes apart.
    const front = placed.rows.find((r) => r.row.row === "frontend");
    expect(front?.ids).toHaveLength(2);
    const [a, b] = (front?.ids ?? []).map((id) => placed.at[id]?.x ?? 0);
    expect((a ?? 0) + (b ?? 0) + 224).toBeCloseTo(0); // symmetric round 0
    // A flow's code nodes by their layer.
    expect(rowOf(PAGE).row).toBe("frontend");
    expect(rowOf(ENDPOINT).row).toBe("api");
    expect(rowOf({ ...SERVICE, layer: "nonsense" }).row).toBe("other");
    const svg = exportSystemSvg(spec, spec.edges, placed);
    expect(svg).toContain("PostgreSQL");
    expect(svg).toContain('stroke-dasharray="6 4"'); // the user's line, inferred
  });

  it("draws an ER diagram's relations from the key's row to the named column", () => {
    const spec = appSchema("Schema");
    const [item, user] = spec.nodes;
    const [owner] = spec.edges;
    if (!item || !user || !owner) throw new Error("the mock's schema");
    expect(tableHeight(item)).toBe(44 + 5 * 26 + 8);
    expect(relationEnds(owner, user)).toEqual({
      sourceHandle: "out:owner_id",
      targetHandle: "in:id",
    });
    expect(relationEnds({ ...owner, label: "owner_id → uuid" }, user).targetHandle).toBe("in:head");
    expect(erGraph(spec, spec.edges).edges).toHaveLength(1);
    const svg = exportErSvg(spec, spec.edges, {
      "table item": { x: 0, y: 0 },
      "table user": { x: 400, y: 0 },
    });
    expect(svg).toContain("FK owner_id");
    expect(svg).toContain("PK id");
    expect(svg).toContain("str?"); // nullable
    expect(viewsOf("er")).toEqual(["er"]);
    expect(viewsOf("flowchart")).toEqual(["flowchart", "sequence", "architecture"]);
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
