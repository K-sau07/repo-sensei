/**
 * Diagrams (6.1, 6.6): a flow drawn from the code graph and checked against it. The API's spec
 * as the canvas reads it, and what the canvas works out of it: how much the graph proves, the
 * edges shown when the unverified ones are hidden, the citations to check for staleness, and
 * what a regenerated diagram added and took away.
 */
import type { Schemas } from "./types";

export type Diagram = Schemas["DiagramOut"];
export type DiagramSummary = Schemas["DiagramSummary"];
export type DiagramKind = Diagram["kind"];
export type DiagramInput = Schemas["DiagramIn"];

export const DIAGRAM_KINDS: readonly DiagramKind[] = ["flowchart", "sequence", "architecture"];

export const KIND_LABELS: Record<DiagramKind, string> = {
  flowchart: "Flowchart",
  sequence: "Sequence",
  architecture: "Architecture",
};

export interface Citation {
  path: string;
  start_line: number;
  end_line: number;
}

export interface SpecNode {
  id: string; // the node's symbol key
  label: string;
  layer: string;
  kind: string;
  citation: Citation | null;
}

export interface SpecEdge {
  from: string;
  to: string;
  type: string | null; // the graph's edge type (null: inferred)
  label: string | null;
  verified: boolean;
  confidence: number | null;
  provenance: string | null;
  inferred: { reason: string } | null;
}

export interface Spec {
  title: string;
  kind: DiagramKind;
  nodes: SpecNode[];
  edges: SpecEdge[];
}

/** The spec as the API sends it (an object of any shape, typed by its writer). */
export function specOf(diagram: Pick<Diagram, "spec">): Spec {
  const raw = diagram.spec as Partial<Spec>;
  return {
    title: raw.title ?? "",
    kind: raw.kind ?? "flowchart",
    nodes: raw.nodes ?? [],
    edges: raw.edges ?? [],
  };
}

/** "84%": the share of edges the graph proves, as the header says it. */
export function verifiedPercent(ratio: number): string {
  return `${String(Math.round(ratio * 100))}%`;
}

/** The edges drawn: all, or only the verified ones. */
export function shownEdges(spec: Spec, hideUnverified: boolean): SpecEdge[] {
  return hideUnverified ? spec.edges.filter((edge) => edge.verified) : spec.edges;
}

/** A node's file and lines: `crud.py:12–40`. */
export function citationLabel(citation: Citation | null): string | null {
  if (!citation) return null;
  const name = citation.path.slice(citation.path.lastIndexOf("/") + 1);
  return citation.end_line > citation.start_line
    ? `${name}:${String(citation.start_line)}–${String(citation.end_line)}`
    : `${name}:${String(citation.start_line)}`;
}

/** What the staleness check is asked about: each cited file once, at the diagram's commit. */
export function citedFiles(spec: Spec, sha: string): { sha: string; path: string }[] {
  const paths = new Set(spec.nodes.flatMap((node) => (node.citation ? [node.citation.path] : [])));
  return [...paths].sort().map((path) => ({ sha, path }));
}

/** What a regenerated diagram added (its nodes the old one hadn't) and took away. */
export function specDiff(before: Spec, after: Spec): { added: Set<string>; removed: SpecNode[] } {
  const old = new Set(before.nodes.map((node) => node.id));
  const now = new Set(after.nodes.map((node) => node.id));
  return {
    added: new Set(after.nodes.filter((node) => !old.has(node.id)).map((node) => node.id)),
    removed: before.nodes.filter((node) => !now.has(node.id)),
  };
}

/** The diagram as text, for the outline view and screen readers: its steps in order. */
export function outline(spec: Spec): string[] {
  const label = new Map(spec.nodes.map((node) => [node.id, node.label]));
  return spec.edges.map((edge) => {
    const from = label.get(edge.from) ?? edge.from;
    const to = label.get(edge.to) ?? edge.to;
    const what = edge.label ? `: ${edge.label}` : "";
    const how = edge.verified ? "" : ` (inferred: ${edge.inferred?.reason ?? "not in the graph"})`;
    return `${from} → ${to}${what}${how}`;
  });
}

/** The request that draws a diagram again, as it was first drawn. */
export function redrawInput(diagram: Diagram): DiagramInput | null {
  const prompt = diagram.prompt as {
    question?: string;
    from_trace?: DiagramInput["from_trace"];
  };
  if (prompt.question) return { question: prompt.question, kind: diagram.kind };
  if (prompt.from_trace) return { from_trace: prompt.from_trace, kind: diagram.kind };
  return null;
}
