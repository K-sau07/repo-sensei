/**
 * Diagrams (6.1, 6.6): a flow drawn from the code graph and checked against it. The API's spec
 * as the canvas reads it, and what the canvas works out of it: how much the graph proves, the
 * edges shown when the unverified ones are hidden, the citations to check for staleness, and
 * what a regenerated diagram added and took away. An ER diagram is a schema's (its tables,
 * their columns and foreign keys); the others are views of a flow, each drawable as another.
 */
import type { Schemas } from "./types";

export type Diagram = Schemas["DiagramOut"];
export type DiagramSummary = Schemas["DiagramSummary"];
export type DiagramKind = Diagram["kind"];
export type DiagramInput = Schemas["DiagramIn"];

export const DIAGRAM_KINDS: readonly DiagramKind[] = [
  "flowchart",
  "sequence",
  "architecture",
  "er",
];
/** The views a flow can be drawn in (an ER diagram is drawn only as one). */
export const FLOW_VIEWS: readonly DiagramKind[] = ["flowchart", "sequence", "architecture"];

export const KIND_LABELS: Record<DiagramKind, string> = {
  flowchart: "Flowchart",
  sequence: "Sequence",
  architecture: "Architecture",
  er: "ER diagram",
};

/** The views a diagram can be shown in: a flow's three, an ER diagram's own. */
export function viewsOf(kind: DiagramKind): readonly DiagramKind[] {
  return kind === "er" ? ["er"] : FLOW_VIEWS;
}

export interface Citation {
  path: string;
  start_line: number;
  end_line: number;
}

/** Something a node holds or uses, as an entity diagram lists it. */
export interface SpecMember {
  name: string;
  /** method, function, variable, class, column, hook, renders, module; a table's pk, fk. */
  kind: string;
  type?: string; // a column's
  ref?: string; // the column a foreign key names: "user.id"
  nullable?: boolean;
}

export interface SpecNode {
  id: string; // the node's symbol key
  label: string;
  layer: string;
  kind: string;
  citation: Citation | null;
  /** Its members (diagrams drawn before 6.6.2 have none). */
  members?: SpecMember[];
  more_members?: number;
  /** An architecture's part: what it does, its icon, how much code it is. */
  note?: string;
  icon?: string;
  size?: number;
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
  /** What the system does, in a sentence (a whole app's architecture). */
  summary?: string;
}

/** The spec as the API sends it (an object of any shape, typed by its writer). */
export function specOf(diagram: Pick<Diagram, "spec">): Spec {
  const raw = diagram.spec as Partial<Spec>;
  return {
    title: raw.title ?? "",
    kind: raw.kind ?? "flowchart",
    nodes: raw.nodes ?? [],
    edges: raw.edges ?? [],
    ...(raw.summary ? { summary: raw.summary } : {}),
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
    whole_app?: boolean;
    whole_schema?: boolean;
  };
  if (prompt.whole_app) return { whole_app: true, kind: "architecture" };
  if (prompt.whole_schema) return { whole_schema: true, kind: "er" };
  if (prompt.question) return { question: prompt.question, kind: diagram.kind };
  if (prompt.from_trace) return { from_trace: prompt.from_trace, kind: diagram.kind };
  return null;
}

/** An edge's words: its label, else its type read aloud (`issues_request`: "issues request"). */
export function edgeWords(edge: SpecEdge): string {
  if (edge.label) return edge.label;
  if (edge.type) return edge.type.replaceAll("_", " ");
  return edge.inferred ? "inferred" : "";
}

/** The order to read a flow in: from its starts (no edge into them), along its edges in the
 * order the diagram gives them, then whatever is left. Each node's place, from 1. */
export function stepOrder(spec: Spec, edges: readonly SpecEdge[]): Map<string, number> {
  const into = new Set(edges.map((edge) => edge.to));
  const next = new Map<string, string[]>();
  for (const edge of edges) next.set(edge.from, [...(next.get(edge.from) ?? []), edge.to]);
  const order = new Map<string, number>();
  const queue = spec.nodes.filter((node) => !into.has(node.id)).map((node) => node.id);
  for (let at = queue.shift(); at !== undefined; at = queue.shift()) {
    if (order.has(at)) continue;
    order.set(at, order.size + 1);
    queue.push(...(next.get(at) ?? []));
  }
  for (const node of spec.nodes) if (!order.has(node.id)) order.set(node.id, order.size + 1);
  return order;
}

/** Where a flow starts (no edge into it) and ends (none out of it). */
export function flowEnds(spec: Spec, edges: readonly SpecEdge[]): Map<string, "start" | "end"> {
  const into = new Set(edges.map((edge) => edge.to));
  const out = new Set(edges.map((edge) => edge.from));
  const ends = new Map<string, "start" | "end">();
  if (edges.length === 0) return ends;
  for (const node of spec.nodes) {
    if (!into.has(node.id) && out.has(node.id)) ends.set(node.id, "start");
    else if (into.has(node.id) && !out.has(node.id)) ends.set(node.id, "end");
  }
  return ends;
}
