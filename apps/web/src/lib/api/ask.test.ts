import { describe, expect, it } from "vitest";

import {
  activity,
  askTransport,
  chipLabel,
  citationsOf,
  lastQuestion,
  sourcesByFile,
  statusLine,
  withMarkers,
  type AskMessage,
  type CitationData,
} from "./ask";

function cite(n: number, path: string, lines: [number, number], span: [number, number]) {
  const data: CitationData = {
    n,
    repo: "o/items",
    sha: "b".repeat(40),
    path,
    lines,
    symbolKey: null,
    span,
    provenance: "evidence",
    verified: n === 1,
  };
  return { type: "data-citation" as const, id: `citation-${n}`, data };
}

const ANSWER: AskMessage = {
  id: "m1",
  role: "assistant",
  parts: [
    { type: "step-start" },
    {
      type: "tool-read_code",
      toolCallId: "t1",
      state: "output-available",
      input: {},
      output: {},
    },
    {
      type: "tool-read_code",
      toolCallId: "t2",
      state: "output-available",
      input: {},
      output: {},
    },
    { type: "tool-grep", toolCallId: "t3", state: "input-available", input: {} },
    { type: "text", text: "The route calls create_item. It saves the item." },
    cite(2, "backend/app/services/items.py", [1, 4], [29, 47]),
    cite(1, "backend/app/api/routes/items.py", [4, 7], [0, 28]),
    cite(3, "backend/app/services/items.py", [3, 3], [29, 47]),
  ],
};

describe("an answer as the page reads it", () => {
  it("puts each citation's marker after the words it backs", () => {
    const text = withMarkers(
      "The route calls create_item. It saves the item.",
      citationsOf(ANSWER),
    );
    expect(text).toBe(
      "The route calls create_item.[1](#cite-1) It saves the item.[2](#cite-2)[3](#cite-3)",
    );
  });

  it("labels a chip by the file and its lines", () => {
    expect(chipLabel("backend/app/services/items.py", [1, 4])).toBe("items.py:1–4");
    expect(chipLabel("README.md", [3, 3])).toBe("README.md:3");
  });

  it("says what the run did, tool by tool", () => {
    expect(activity(ANSWER)).toEqual([
      { label: "Reading files", count: 2 },
      { label: "Searching text", count: 1 },
    ]);
    expect(statusLine(ANSWER, true)).toBe("Reading files ×2 · Searching text · Writing the answer");
    expect(statusLine(undefined, true)).toBe("Thinking");
  });

  it("groups the sources by file, in the order first cited", () => {
    expect(sourcesByFile(ANSWER).map((f) => [f.path, f.citations.map((c) => c.n)])).toEqual([
      ["backend/app/api/routes/items.py", [1]],
      ["backend/app/services/items.py", [2, 3]],
    ]);
  });
});

describe("the transport", () => {
  it("sends the question alone, its conversation and mode, with the CSRF header", async () => {
    const sent: { body: unknown; headers: Headers; url: string }[] = [];
    const transport = askTransport({ org: "acme", repo: "items", getCsrfToken: () => "tok" });
    const fetched: typeof fetch = (input, init) => {
      const url = input instanceof Request ? input.url : input.toString();
      const body = typeof init?.body === "string" ? init.body : "";
      sent.push({ url, body: JSON.parse(body), headers: new Headers(init?.headers) });
      return Promise.resolve(
        new Response("data: [DONE]\n\n", { headers: { "content-type": "text/event-stream" } }),
      );
    };
    Object.assign(transport, { fetch: fetched });
    const messages = [
      { id: "u1", role: "user", parts: [{ type: "text", text: "Where?" }] },
      { id: "a1", role: "assistant", parts: [{ type: "text", text: "Here." }] },
      { id: "u2", role: "user", parts: [{ type: "text", text: "And deleted?" }] },
    ] as AskMessage[];
    expect(lastQuestion(messages)).toBe("And deleted?");
    await transport.sendMessages({
      chatId: "c",
      messages,
      abortSignal: undefined,
      trigger: "submit-message",
      messageId: undefined,
      body: { conversationId: "conv-1", mode: "deep" },
    });
    expect(sent).toHaveLength(1);
    expect(sent[0]?.url).toBe("/api/v1/orgs/acme/repos/items/ask");
    expect(sent[0]?.body).toEqual({
      message: "And deleted?",
      conversation_id: "conv-1",
      mode: "deep",
    });
    expect(sent[0]?.headers.get("X-CSRF-Token")).toBe("tok");
  });
});
