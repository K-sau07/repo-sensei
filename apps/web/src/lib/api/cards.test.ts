import { describe, expect, it } from "vitest";

import { MOCK_CARDS, mockAnswerParts } from "@/mocks/ask";

import type { AskMessage } from "./ask";
import { cardKey, cardsOf, cardTitle, endpointsOf, followUps, type ToolCard } from "./cards";

const answer = (parts: Record<string, unknown>[]): AskMessage =>
  ({ id: "m", role: "assistant", parts }) as unknown as AskMessage;

/** The mock stream's parts as the client holds them: each tool call one part, its output in. */
function assembled(): AskMessage {
  const parts: Record<string, unknown>[] = [];
  const calls = new Map<string, Record<string, unknown>>();
  for (const part of mockAnswerParts("c1")) {
    if (part.type === "tool-input-available") {
      const call = {
        type: `tool-${String(part.toolName)}`,
        toolCallId: part.toolCallId,
        state: "input-available",
        input: part.input,
      };
      calls.set(String(part.toolCallId), call);
      parts.push(call);
    } else if (part.type === "tool-output-available") {
      Object.assign(calls.get(String(part.toolCallId)) ?? {}, {
        state: "output-available",
        output: part.output,
      });
    }
  }
  return answer(parts);
}

describe("tool cards", () => {
  it("are the card-making calls, in order, a call that found nothing left out", () => {
    const slots = cardsOf(assembled());
    expect(slots.map((s) => [s.tool, s.state, s.card?.kind])).toEqual([
      ["find_symbol", "ready", "symbols"],
      ["graph_neighbors", "ready", "graph"],
      ["trace_feature", "ready", "trace"],
    ]);
    const running = answer([
      { type: "tool-trace_feature", toolCallId: "a", state: "input-available" },
      { type: "tool-find_symbol", toolCallId: "b", state: "output-available", output: {} },
      {
        type: "tool-graph_neighbors",
        toolCallId: "c",
        state: "output-available",
        output: { isError: true, summary: "not in the graph" },
      },
    ]);
    expect(cardsOf(running).map((s) => [s.id, s.state, s.summary])).toEqual([
      ["a", "loading", ""],
      ["c", "error", "not in the graph"],
    ]);
  });

  it("have titles, and keys a later answer's same card is versioned under", () => {
    const cards = Object.values(MOCK_CARDS) as unknown as ToolCard[];
    expect(cards.map(cardTitle)).toEqual([
      "Definitions (2)",
      "Neighbours of create_item",
      "Trace from AddItem",
    ]);
    const [, graph] = cards;
    expect(graph && cardKey(graph)).toBe(`graph:${MOCK_CARDS.graph.root.symbol_key}`);
  });

  it("list the endpoints they pass through, once, with what serves each", () => {
    const rows = endpointsOf(cardsOf(assembled()));
    expect(rows.map((r) => [r.method, r.path, r.handler?.name ?? null])).toEqual([
      ["POST", "/api/v1/items/", "create_item"],
    ]);
  });

  it("suggest follow-ups about what they found, none already asked", () => {
    const slots = cardsOf(assembled());
    // The graph card's hops go both ways, so its question is the callers' one: once.
    expect(followUps(slots, [])).toEqual([
      "What calls `create_item`?",
      "Where is `Item` tested?",
      "What else calls `POST /api/v1/items/`?",
    ]);
    expect(followUps(slots, ["what calls `create_item`?"])).toEqual([
      "Where is `Item` tested?",
      "What else calls `POST /api/v1/items/`?",
    ]);
  });
});
