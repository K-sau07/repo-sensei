/**
 * The mock Ask answer: the UI Message Stream the API sends for "how is an item created?" on the
 * mock snapshot (the fastapi template), part by part as the encoder writes them
 * (apps/api/src/sensei_api/ui_stream.py): a research turn that reads the route and the model
 * and commits to them, then the answer turn's text, its two citations, its confidence and what
 * it searched. The citations' lines are the mock snapshot's.
 */
import type { CitationData } from "@/lib/api/ask";

export const MOCK_SHA = "b1125d8" + "0".repeat(33);
const ROUTE = "backend/app/api/routes/items.py";
const MODEL = "backend/app/models.py";
export const MOCK_ANSWER =
  "An item is created by the `create_item` route: it validates the payload, sets the owner to the " +
  "signed-in user and adds the row to the session. The row is an `Item`, the SQLModel table of " +
  "items.";

function citation(n: number, path: string, lines: [number, number], span: [number, number]) {
  const data: CitationData = {
    n,
    repo: "fastapi/full-stack-fastapi-template",
    sha: MOCK_SHA,
    path,
    lines,
    symbolKey: null,
    span,
    provenance: "evidence",
    verified: true,
  };
  return { type: "data-citation", id: `citation-${n}`, data };
}

const FIRST = MOCK_ANSWER.indexOf(". The row") + 1;

/** The stream's parts for one answer, after its `start` (the message's id is the caller's). */
export function mockAnswerParts(conversationId: string): Record<string, unknown>[] {
  const tool = (id: string, name: string, input: Record<string, unknown>, summary: string) => [
    { type: "tool-input-start", toolCallId: id, toolName: name },
    { type: "tool-input-available", toolCallId: id, toolName: name, input },
    {
      type: "tool-output-available",
      toolCallId: id,
      output: { summary, hits: 1, isError: false, ms: 40 },
    },
  ];
  const words = MOCK_ANSWER.split(/(?<= )/);
  return [
    { type: "data-conversation", id: "conversation", data: { id: conversationId, runId: null } },
    { type: "start-step" },
    ...tool("t1", "code_search", { query: "create item" }, "6 results in 4 files"),
    ...tool("t2", "read_code", { path: ROUTE, start_line: 55, end_line: 80 }, `${ROUTE}:55-80`),
    ...tool("t3", "read_code", { path: MODEL, start_line: 80, end_line: 95 }, `${MODEL}:80-95`),
    { type: "finish-step" },
    { type: "start-step" },
    { type: "text-start", id: "text-2" },
    ...words.map((delta) => ({ type: "text-delta", id: "text-2", delta })),
    { type: "text-end", id: "text-2" },
    citation(1, ROUTE, [62, 72], [0, FIRST]),
    citation(2, MODEL, [87, 93], [FIRST, MOCK_ANSWER.length]),
    {
      type: "data-confidence",
      id: "confidence",
      data: {
        level: "high",
        score: 0.92,
        breakdown: {
          claims_total: 2,
          claims_cited: 2,
          claims_verified: 2,
          claims_partial: 0,
          inferred: 0,
          unresolved_hops: 0,
          agreement: null,
          unknown_identifiers: 0,
          dropped_citations: 0,
          whole: true,
        },
      },
    },
    {
      type: "data-searchlog",
      id: "searchlog",
      data: {
        queries: ["code_search 'create item'", `read_code '${ROUTE}'`, `read_code '${MODEL}'`],
      },
    },
    { type: "finish-step" },
    { type: "finish" },
  ];
}
