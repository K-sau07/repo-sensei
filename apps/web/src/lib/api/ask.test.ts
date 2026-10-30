import { describe, expect, it } from "vitest";

import {
  activity,
  askTransport,
  chipLabel,
  citationsOf,
  confidenceLines,
  lastQuestion,
  safeHref,
  sourcesByFile,
  statusLine,
  titleOr,
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

  it("counts the spans in code points, as the API does: an emoji is one", () => {
    const text = "🔐 Logins are checked in `verify_token`. Then saved.";
    // eslint-disable-next-line @typescript-eslint/no-misused-spread -- code points, on purpose
    const end = [...text].indexOf("T"); // the second sentence, in code points
    const marked = withMarkers(text, [cite(1, "a.py", [1, 2], [0, end - 1]).data]);
    expect(marked).toBe("🔐 Logins are checked in `verify_token`.[1](#cite-1) Then saved.");
  });

  it("opens only the web's links, mail and the page's own", () => {
    expect(safeHref("https://example.com/a")).toBe("https://example.com/a");
    expect(safeHref("mailto:a@example.com")).toBe("mailto:a@example.com");
    expect(safeHref("#cite-2")).toBe("#cite-2");
    expect(safeHref("/acme/api/code")).toBe("/acme/api/code");
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("JaVaScRiPt:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,x")).toBeNull();
    expect(safeHref("//evil.example")).toBeNull();
    expect(safeHref(undefined)).toBeNull();
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

describe("how sure an answer is", () => {
  const breakdown = {
    claims_total: 5,
    claims_cited: 4,
    claims_verified: 4,
    claims_partial: 0,
    inferred: 1,
    unresolved_hops: 0,
    agreement: null,
    unknown_identifiers: 2,
    dropped_citations: 1,
    whole: true,
  };

  it("says what was checked, and only what went wrong besides", () => {
    expect(confidenceLines(breakdown)).toEqual([
      "4/5 claims verified against the code",
      "1 without a citation",
      "1 inferred from naming, not followed in the code",
      "2 names not found in the code",
      "1 citation dropped as unsupported",
    ]);
  });

  it("says when there was nothing to check, and a part left unanswered", () => {
    expect(
      confidenceLines({
        ...breakdown,
        claims_total: 0,
        claims_cited: 0,
        claims_verified: 0,
        inferred: 0,
        unknown_identifiers: 0,
        dropped_citations: 0,
        unresolved_hops: 1,
        agreement: 0.5,
        whole: false,
      }),
    ).toEqual([
      "No claims to check",
      "1 step not traced",
      "Two searches agreed on 50%",
      "Parts of the question may be unanswered",
    ]);
  });

  it("titles an untitled conversation", () => {
    expect(titleOr("", "Untitled")).toBe("Untitled");
    expect(titleOr(undefined, "New conversation")).toBe("New conversation");
    expect(titleOr("Saving items", "Untitled")).toBe("Saving items");
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

describe("a question's scope", () => {
  it("reads as its path and its layers", async () => {
    const { scopeLabel, scopeOrNull } = await import("./ask");
    const file = { path: "backend/app/api/routes/items.py", startLine: 62, endLine: 72 };
    expect(scopeLabel(file)).toBe("items.py:62–72");
    expect(scopeLabel({ path: "backend/app", startLine: null, endLine: null })).toBe("app/");
    expect(scopeLabel({ path: null, startLine: null, endLine: null, layers: ["api", "db"] })).toBe(
      "API · Data",
    );
    expect(scopeOrNull({ path: null, startLine: null, endLine: null, layers: [] })).toBeNull();
  });

  it("finds the @ mention being typed before the caret", async () => {
    const { mentionAt } = await import("@/components/ask/composer");
    expect(mentionAt("what calls @crea", 16)).toEqual({ start: 11, query: "crea" });
    expect(mentionAt("@", 1)).toEqual({ start: 0, query: "" });
    expect(mentionAt("mail me@example.com", 19)).toBeNull(); // not after a space
    expect(mentionAt("what calls @crea and", 20)).toBeNull(); // the caret moved past it
  });
});

describe("a logged search", () => {
  it("is its tool and query, as the run wrote it (Python's quoting)", async () => {
    const { searchOf } = await import("./ask");
    expect(searchOf("code_search 'create item'")).toEqual({
      tool: "code_search",
      query: "create item",
    });
    expect(searchOf(`grep "user's id"`)).toEqual({ tool: "grep", query: "user's id" });
    // Python's escapes, undone: the regex as it was asked.
    expect(searchOf(String.raw`grep '\\bfoo\\b'`)).toEqual({
      tool: "grep",
      query: String.raw`\bfoo\b`,
    });
    expect(searchOf(String.raw`grep 'it\'s'`)).toEqual({ tool: "grep", query: "it's" });
    expect(searchOf(`code_search '${"a".repeat(80)}'`)).toBeNull(); // maybe cut short
    expect(searchOf("read_code 'backend/app/models.py'")).toBeNull(); // not a search
    expect(searchOf("list_dir")).toBeNull();
  });
});
