/**
 * What the agent's tools found, as cards beside an answer (4.11.3): the definitions
 * `find_symbol` found, the neighbours `graph_neighbors` walked (as the graph API sends them),
 * the paths `trace_feature` traced (sensei_engine.agent.cards), and the endpoints among them.
 * And follow-up questions about what they found, so a follow-up names real code.
 */
import type { AskMessage } from "./ask";
import type { GraphHop, GraphNode } from "./graph";

export interface SymbolEntry {
  symbolKey: string;
  name: string;
  qualifiedName: string;
  kind: string;
  path: string;
  lines: [number, number];
  signature: string | null;
}

export interface SymbolsCard {
  kind: "symbols";
  symbols: SymbolEntry[];
  more: number;
}

export interface GraphCard {
  kind: "graph";
  root: GraphNode;
  hops: GraphHop[];
  more: number;
}

export interface TraceCardEdge {
  type: string;
  confidence: number;
  provenance: string;
  evidence: { path: string; line: number | null } | null;
}

export interface TraceCardHop {
  layer: string;
  node: GraphNode;
  edge: TraceCardEdge | null;
}

export interface TraceCardPath {
  confidence: number;
  reachesGoal: boolean;
  layers: string[];
  hops: TraceCardHop[];
  cut: number;
  breaks: { afterHop: number; reason: string; kind: string }[];
}

export interface TraceCard {
  kind: "trace";
  entry: string;
  paths: TraceCardPath[];
  more: number;
}

export type ToolCard = SymbolsCard | GraphCard | TraceCard;

/** A card-making tool call of the message: still running, done with its card, or failed. */
export interface CardSlot {
  id: string;
  tool: string;
  state: "loading" | "ready" | "error";
  card: ToolCard | null;
  summary: string;
}

const CARD_TOOLS = new Set(["find_symbol", "graph_neighbors", "trace_feature"]);

interface ToolPart {
  type: string;
  toolCallId?: string;
  state?: string;
  output?: { card?: ToolCard; isError?: boolean; summary?: string };
}

/** The message's card-making tool calls, in the order the run made them. A call done without a
 * card (it found nothing) is left out; a call still running is a slot to load in. */
export function cardsOf(message: AskMessage): CardSlot[] {
  const slots: CardSlot[] = [];
  for (const raw of message.parts) {
    if (!raw.type.startsWith("tool-")) continue;
    const tool = raw.type.slice("tool-".length);
    if (!CARD_TOOLS.has(tool)) continue;
    const part = raw as ToolPart;
    const id = part.toolCallId ?? `${tool}-${String(slots.length)}`;
    const output = part.output;
    if (part.state !== "output-available" || output === undefined) {
      slots.push({ id, tool, state: "loading", card: null, summary: "" });
    } else if (output.isError) {
      slots.push({ id, tool, state: "error", card: null, summary: output.summary ?? "" });
    } else if (output.card) {
      slots.push({ id, tool, state: "ready", card: output.card, summary: output.summary ?? "" });
    }
  }
  return slots;
}

/** A card's title, and the key an artifact is versioned under (the same card asked again). */
export function cardTitle(card: ToolCard): string {
  switch (card.kind) {
    case "symbols":
      return card.symbols.length === 1 && card.symbols[0]
        ? `Definition of ${card.symbols[0].name}`
        : `Definitions (${String(card.symbols.length + card.more)})`;
    case "graph":
      return `Neighbours of ${card.root.name}`;
    case "trace":
      return `Trace from ${card.paths[0]?.hops[0]?.node.name ?? card.entry}`;
  }
}

export function cardKey(card: ToolCard): string {
  switch (card.kind) {
    case "symbols":
      return `symbols:${card.symbols.map((s) => s.symbolKey).join(",")}`;
    case "graph":
      return `graph:${card.root.symbol_key}`;
    case "trace":
      return `trace:${card.paths[0]?.hops[0]?.node.symbol_key ?? card.entry}`;
  }
}

export interface EndpointRow {
  method: string;
  path: string;
  node: GraphNode;
  handler: GraphNode | null; // what serves it, when a card shows the hop after it
}

const ENDPOINT = /^([A-Z]+)\s+(\S+)$/;

/** The API endpoints the message's cards pass through, each once, with its handler. */
export function endpointsOf(slots: readonly CardSlot[]): EndpointRow[] {
  const rows = new Map<string, EndpointRow>();
  const add = (node: GraphNode, handler: GraphNode | null) => {
    if (node.kind !== "api_endpoint") return;
    const named = ENDPOINT.exec(node.name);
    if (!named?.[1] || !named[2]) return;
    const known = rows.get(node.symbol_key);
    if (known && (known.handler || !handler)) return;
    rows.set(node.symbol_key, { method: named[1], path: named[2], node, handler });
  };
  for (const { card } of slots) {
    if (card?.kind === "trace") {
      for (const path of card.paths) {
        path.hops.forEach((hop, i) => {
          add(hop.node, path.hops[i + 1]?.node ?? null);
        });
      }
    } else if (card?.kind === "graph") {
      add(card.root, null);
      for (const hop of card.hops) add(hop.node, null);
    }
  }
  return [...rows.values()];
}

const MAX_FOLLOW_UPS = 3;

/** Questions to ask next about what the run found (its cards' symbols, the end of its trace,
 * its endpoints), none asked already in the thread. */
export function followUps(slots: readonly CardSlot[], asked: readonly string[]): string[] {
  const found: string[] = [];
  for (const { card } of slots) {
    if (card?.kind === "symbols" && card.symbols[0]) {
      found.push(`What calls \`${card.symbols[0].name}\`?`);
    } else if (card?.kind === "graph") {
      const inward = card.hops.length > 0 && card.hops.every((h) => h.edge.direction === "in");
      found.push(
        inward ? `What does \`${card.root.name}\` depend on?` : `What calls \`${card.root.name}\`?`,
      );
    } else if (card?.kind === "trace") {
      const best = card.paths[0];
      const last = best?.hops.at(-1);
      if (best && last && !best.reachesGoal) {
        found.push(`Why does the trace stop at \`${last.node.name}\`?`);
      } else if (last) {
        found.push(`Where is \`${last.node.name}\` tested?`);
      }
    }
  }
  for (const endpoint of endpointsOf(slots)) {
    found.push(`What else calls \`${endpoint.method} ${endpoint.path}\`?`);
  }
  const seen = new Set(asked.map((q) => q.trim().toLowerCase()));
  const out: string[] = [];
  for (const question of found) {
    const key = question.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(question);
    if (out.length === MAX_FOLLOW_UPS) break;
  }
  return out;
}
