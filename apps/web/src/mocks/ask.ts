/**
 * The mock Ask answer: the UI Message Stream the API sends for "how is an item created?" on the
 * mock snapshot (the fastapi template), part by part as the encoder writes them
 * (apps/api/src/sensei_api/ui_stream.py): a research turn that reads the route and the model
 * and commits to them, then the answer turn's text, its two citations, its confidence and what
 * it searched. The citations' lines are the mock snapshot's.
 */
import type { CitationData } from "@/lib/api/ask";
import { BROWSE_COMMIT } from "@/mocks/browse";
import { MOCK_RUN_ID } from "@/mocks/runs";

export const MOCK_SHA = BROWSE_COMMIT; // the mock snapshot's: what its citations are read at
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
    {
      type: "data-conversation",
      id: "conversation",
      data: { id: conversationId, runId: MOCK_RUN_ID },
    },
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

export const MOCK_ABSTAIN_TEXT =
  "I couldn't find where payments are refunded: nothing in this repository handles a refund.";

/** An answer the run couldn't stand behind: it searched, the trail ended, and it says so. */
export function mockAbstainParts(conversationId: string): Record<string, unknown>[] {
  return [
    {
      type: "data-conversation",
      id: "conversation",
      data: { id: conversationId, runId: MOCK_RUN_ID },
    },
    { type: "start-step" },
    { type: "tool-input-start", toolCallId: "t1", toolName: "code_search" },
    {
      type: "tool-input-available",
      toolCallId: "t1",
      toolName: "code_search",
      input: { query: "refund payment" },
    },
    {
      type: "tool-output-available",
      toolCallId: "t1",
      output: { summary: "no results", hits: 0, isError: false, ms: 30 },
    },
    { type: "finish-step" },
    { type: "start-step" },
    { type: "text-start", id: "text-2" },
    { type: "text-delta", id: "text-2", delta: MOCK_ABSTAIN_TEXT },
    { type: "text-end", id: "text-2" },
    {
      type: "data-confidence",
      id: "confidence",
      data: {
        level: "low",
        score: 0.18,
        breakdown: {
          claims_total: 0,
          claims_cited: 0,
          claims_verified: 0,
          claims_partial: 0,
          inferred: 0,
          unresolved_hops: 1,
          agreement: null,
          unknown_identifiers: 0,
          dropped_citations: 0,
          whole: false,
        },
      },
    },
    {
      type: "data-searchlog",
      id: "searchlog",
      data: { queries: ["code_search 'refund payment'"] },
    },
    {
      type: "data-abstain",
      id: "abstain",
      data: {
        reason: "No code it read handles a refund, so there is nothing to cite.",
        searched: ["code_search 'refund payment'", "grep 'refund'"],
        trailEnd: { end: "POST /items/", reason: "no call to a payment service" },
        suggestion: "Where are items deleted?",
      },
    },
    { type: "finish-step" },
    { type: "finish" },
  ];
}
