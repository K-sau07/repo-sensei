/**
 * A sequence diagram's geometry (6.6.2): its participants as columns, in the order they first
 * take part, each with a lifeline down the page; its messages as rows, in the diagram's order,
 * each an arrow from one lifeline to another (a loop back to its own for a call to itself),
 * numbered. The view and the exports draw from the same numbers.
 */
import type { Spec, SpecEdge, SpecNode } from "@/lib/api/diagrams";

export const COLUMN = 196; // a participant's column, across
export const CARD_WIDTH = 180;
export const CARD_HEIGHT = 76;
export const ROW = 56; // a message's row, down
export const TOP = CARD_HEIGHT + 32; // where the first message is drawn
export const LOOP = 28; // a call to itself: how far right its loop goes

export interface Participant {
  node: SpecNode;
  x: number; // its lifeline's
}

export interface Message {
  n: number; // from 1
  edge: SpecEdge;
  from: number; // the lifelines' x
  to: number;
  y: number;
  self: boolean;
}

export interface Sequence {
  participants: Participant[];
  messages: Message[];
  width: number;
  height: number;
}

export function sequenceOf(spec: Spec, edges: readonly SpecEdge[]): Sequence {
  const byId = new Map(spec.nodes.map((node) => [node.id, node]));
  const order: string[] = [];
  const take = (id: string) => {
    if (byId.has(id) && !order.includes(id)) order.push(id);
  };
  for (const edge of edges) {
    take(edge.from);
    take(edge.to);
  }
  for (const node of spec.nodes) take(node.id);
  const x = new Map(order.map((id, i) => [id, COLUMN * i + COLUMN / 2]));
  const participants = order.flatMap((id) => {
    const node = byId.get(id);
    return node ? [{ node, x: x.get(id) ?? 0 }] : [];
  });
  const messages = edges
    .filter((edge) => x.has(edge.from) && x.has(edge.to))
    .map((edge, i) => ({
      n: i + 1,
      edge,
      from: x.get(edge.from) ?? 0,
      to: x.get(edge.to) ?? 0,
      y: TOP + ROW * i + ROW / 2,
      self: edge.from === edge.to,
    }));
  return {
    participants,
    messages,
    width: Math.max(COLUMN, COLUMN * participants.length),
    height: TOP + ROW * Math.max(1, messages.length) + 24,
  };
}
