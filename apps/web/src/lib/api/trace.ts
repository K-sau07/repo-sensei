// The trace endpoint's types (3.12) and what the trace view reads from them.
import { EDGE_VERBS, provenanceTag } from "./graph";
import type { Schemas } from "./types";

export type Trace = Schemas["TraceOut"];
export type TracePath = Schemas["TracePathOut"];
export type TraceHop = Schemas["TraceHopOut"];
export type TraceNode = Schemas["TraceNodeOut"];
export type TraceEdge = Schemas["TraceEdgeOut"];
export type TraceBreak = Schemas["TraceBreakOut"];
export type TraceRequest = Schemas["TraceRequest"];
export type TraceLayer = TraceHop["layer"];
export type TraceEntryKind = TraceRequest["kind"];

/** The swimlanes, top to bottom: the way a request travels, from the UI to the data. */
export const TRACE_LAYERS: readonly TraceLayer[] = [
  "ui",
  "state",
  "client",
  "route",
  "handler",
  "service",
  "db",
  "other",
];

/** The most paths a trace has (the engine's `TOP_PATHS`). */
export const TOP_PATHS = 3;

/** How sure a hop is, as its pip says it: proven by the code, matched by a rule, or a gap. */
export type Pip = "verified" | "inferred" | "unresolved";

/** How a hop's edge was found, as a pip (the entry has no edge: none). */
export function pipOf(edge: TraceEdge | null): Pip | null {
  if (!edge) return null;
  return provenanceTag(edge.provenance) === "precise" ? "verified" : "inferred";
}

/** The hop's call site has several possible targets: this path follows one of them. */
export const isAmbiguous = (edge: TraceEdge | null): edge is TraceEdge =>
  edge !== null && edge.alternatives >= 2;

/** How a hop was reached, from the hop before ("uses", "is handled by", ...). */
export function edgeLabel(edge: TraceEdge): string {
  // An override step: the method the trace reached does nothing, the subclass's does.
  return edge.type === "implements" ? "is implemented by" : EDGE_VERBS[edge.type];
}

/** How a path's hop is reached, in a word or two: where the journey starts (the first hop,
 * when the entry comes later), the entry, or the edge from the hop before ("uses", ...). */
export function howReached(path: TracePath, index: number): string {
  const edge = path.hops[index]?.edge;
  if (edge) return edgeLabel(edge);
  return index === path.entry_index ? "entry" : "starts here";
}

/** Whether a path's hop is the entry traced, after where it is reached from. */
export const isMidEntry = (path: TracePath, index: number) =>
  index === path.entry_index && index > 0;

/** Where a path's journey starts, when the entry is reached from somewhere: a page, or the
 * first code that reaches it when no page does. */
export function pathStart(path: TracePath): string | null {
  if (path.entry_index === 0) return null;
  const first = path.hops[0]?.node.name ?? "";
  return path.reaches_origin ? `Starts at ${first}` : `Reached from ${first}; no page leads there`;
}

export const percent = (confidence: number) => `${String(Math.round(confidence * 100))}%`;

export interface HopPlace {
  path: string;
  start: number;
  end: number;
}

/** Where a hop is shown: the line that proves its edge (the call site), else its own lines (the
 * entry); null for a node in no file. */
export function hopPlace(hop: TraceHop): HopPlace | null {
  const evidence = hop.edge?.evidence;
  if (evidence) {
    const line = Math.max(evidence.line ?? 1, 1);
    return { path: evidence.path, start: line, end: line };
  }
  const { path, lines } = hop.node;
  if (!path) return null;
  const [start, end] = lines ?? [1, 1];
  return { path, start: Math.max(start, 1), end: Math.max(end, start, 1) };
}

/** A place as its chip reads: `path:line`. */
export const placeLabel = (place: HopPlace) => `${place.path}:${String(place.start)}`;

/** Where a hop is defined (a table at its ORM class), for "Open the definition". */
export function definitionPlace(node: TraceNode): HopPlace | null {
  const model = node.model;
  if (model) return { path: model.path, start: model.lines[0], end: model.lines[1] };
  if (!node.path || !node.lines) return null;
  return { path: node.path, start: node.lines[0], end: node.lines[1] };
}

/** Where a path breaks, by the hop it stops after. */
export function breaksByHop(path: TracePath): Map<number, TraceBreak> {
  return new Map(path.breaks.map((found) => [found.after_hop, found]));
}

/** A path in a few words, for its tab: where it starts and where it ends. */
export function pathTitle(path: TracePath): string {
  const first = path.hops[0]?.node.name ?? "";
  const last = path.hops.at(-1)?.node.name ?? "";
  return path.hops.length > 1 ? `${first} → ${last}` : first;
}

/** What a path comes to: it reaches the goal, or where and why it stops. */
export function pathOutcome(path: TracePath, goal: TraceNode | null): string {
  const last = path.hops.at(-1)?.node;
  if (path.reaches_goal) {
    if (goal) return `Reaches ${goal.name}`;
    return last?.kind === "table" ? `Reaches the ${last.name} table` : "Reaches the data";
  }
  const [stop] = path.breaks;
  return stop ? `Stops: ${stop.reason}` : "Stops short of the data";
}

/** The files a path passes through, in order, each once (a table at its ORM class's file). */
export function pathFiles(path: TracePath): string[] {
  const files: string[] = [];
  for (const hop of path.hops) {
    const file = hop.node.model?.path ?? hop.node.path;
    if (file && !files.includes(file)) files.push(file);
  }
  return files;
}

/** The question "Help me resolve" asks about a gap (pasted into Ask until it can be opened). */
export function resolveQuestion(path: TracePath, gap: TraceBreak): string {
  const hop = path.hops[gap.after_hop];
  const entry = path.hops[0]?.node;
  const where = hop ? hopPlace(hop) : null;
  const name = hop ? hop.node.qualified_name || hop.node.name : "this step";
  return [
    `The trace from ${entry?.qualified_name ?? "the entry"} stops after ${name}`,
    where ? ` (${placeLabel(where)})` : "",
    `: ${gap.reason}`,
    gap.detail ? ` (${gap.detail})` : "",
    ". Where does it go next, and what in the code connects them?",
  ].join("");
}
