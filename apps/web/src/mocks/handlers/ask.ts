/**
 * The mock `POST …/ask` (the answer streamed as the UI Message Stream, a part every few
 * milliseconds so it is seen streaming), the user's conversations and one conversation, kept
 * in memory as the page asks.
 *
 * Feedback, renaming, the history's search and sharing (4.11) are kept in memory too; a shared
 * link opens with its citations redacted when the mock session is signed out.
 *
 * Situations worth seeing, from the browser:
 *   localStorage.setItem("sensei.mock.ask", "slow")
 * "slow" (150 ms a part); "abstain" (the run can't stand behind an answer and says so).
 */
import { delay, http, HttpResponse } from "msw";

import { mockAbstainParts, mockAnswerParts } from "@/mocks/ask";
import { apiError } from "@/mocks/handlers/errors";
import { mockScenario } from "@/mocks/scenario";

const ASK_SCENARIO_KEY = "sensei.mock.ask";
const PART_MS = 15;
const SLOW_MS = 150;

interface Stored {
  id: string;
  repo: string;
  title: string;
  messages: {
    id: string;
    role: "user" | "assistant";
    parts: Record<string, unknown>[];
    feedback?: { kind: string; note: string; created_at: string } | null;
  }[];
  updated_at: string;
  token: string | null;
}

const STORE_KEY = "sensei.mock.conversations";

/** Kept in localStorage too, so a share link opened in a new page (signed out) finds them. */
const conversations = new Map<string, Stored>(load());

function load(): [string, Stored][] {
  if (typeof localStorage === "undefined") return [];
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) ?? "[]") as [string, Stored][];
  } catch {
    return [];
  }
}

function save() {
  if (typeof localStorage !== "undefined") {
    localStorage.setItem(STORE_KEY, JSON.stringify([...conversations]));
  }
}

function askScenario(): string | null {
  return typeof localStorage === "undefined" ? null : localStorage.getItem(ASK_SCENARIO_KEY);
}

function slow(): boolean {
  return askScenario() === "slow";
}

function listed(c: Stored) {
  return {
    id: c.id,
    repo_id: "00000000-0000-0000-0000-000000000000",
    title: c.title,
    shared: c.token !== null,
    created_at: c.updated_at,
    updated_at: c.updated_at,
  };
}

function messagesOf(c: Stored, redact = false) {
  return c.messages.map((m) => ({
    id: m.id,
    role: m.role,
    parts: redact ? redacted(m.parts) : m.parts.filter((p) => p.type !== "data-conversation"),
    status: "done",
    confidence: null,
    abstained: false,
    created_at: c.updated_at,
    citations: [],
    feedback: m.feedback ?? null,
  }));
}

/** As the API sends them to a viewer who can't read the repository (sensei_api.sharing). */
function redacted(parts: Record<string, unknown>[]): Record<string, unknown>[] {
  return parts.flatMap((p) => {
    if (p.type === "text" || p.type === "data-confidence") return [p];
    if (p.type === "data-citation") {
      const data = p.data as { n: number; span: [number, number] };
      return [{ type: p.type, id: p.id, data: { n: data.n, span: data.span, redacted: true } }];
    }
    return [];
  });
}

function notFound() {
  return apiError(404, "conversation_not_found", "Conversation not found.");
}

function answerById(id: string) {
  for (const c of conversations.values()) {
    const found = c.messages.find((m) => m.id === id && m.role === "assistant");
    if (found) return found;
  }
  return undefined;
}

/** The parts as the client will hold them: what the conversation keeps. */
function assembled(parts: Record<string, unknown>[]): Record<string, unknown>[] {
  const text = parts
    .filter((p) => p.type === "text-delta")
    .map((p) => String(p.delta))
    .join("");
  return [{ type: "text", text }, ...parts.filter((p) => String(p.type).startsWith("data-"))];
}

export const askHandlers = [
  http.post("/api/v1/orgs/:org/repos/:repo/ask", async ({ request, params }) => {
    const body = (await request.json()) as { message?: string; conversation_id?: string | null };
    const question = body.message ?? "";
    const id = body.conversation_id ?? crypto.randomUUID();
    const messageId = crypto.randomUUID();
    const parts = askScenario() === "abstain" ? mockAbstainParts(id) : mockAnswerParts(id);
    const held = conversations.get(id) ?? {
      id,
      repo: String(params.repo),
      title: question.slice(0, 80),
      messages: [],
      updated_at: new Date().toISOString(),
      token: null,
    };
    held.messages.push(
      { id: crypto.randomUUID(), role: "user", parts: [{ type: "text", text: question }] },
      { id: messageId, role: "assistant", parts: assembled(parts) },
    );
    held.updated_at = new Date().toISOString();
    conversations.set(id, held);
    save();
    const wait = slow() ? SLOW_MS : PART_MS;
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      async start(controller) {
        for (const part of [{ type: "start", messageId }, ...parts]) {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(part)}\n\n`));
          await delay(wait);
        }
        controller.enqueue(encoder.encode("data: [DONE]\n\n"));
        controller.close();
      },
    });
    return new HttpResponse(stream, {
      headers: {
        "content-type": "text/event-stream",
        "x-vercel-ai-ui-message-stream": "v1",
        "x-conversation-id": id,
      },
    });
  }),
  http.get("/api/v1/orgs/:org/repos/:repo/conversations", ({ params, request }) => {
    const q = (new URL(request.url).searchParams.get("q") ?? "").toLowerCase();
    return HttpResponse.json({
      conversations: [...conversations.values()]
        .filter((c) => c.repo === params.repo && c.title.toLowerCase().includes(q))
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .map(listed),
      next_cursor: null,
    });
  }),
  http.get("/api/v1/orgs/:org/conversations/:id", ({ params }) => {
    const found = conversations.get(String(params.id));
    if (!found) return notFound();
    return HttpResponse.json({ ...listed(found), messages: messagesOf(found) });
  }),
  http.patch("/api/v1/orgs/:org/conversations/:id", async ({ params, request }) => {
    const found = conversations.get(String(params.id));
    if (!found) return notFound();
    const body = (await request.json()) as { title: string };
    found.title = body.title.split(/\s+/).filter(Boolean).join(" ");
    save();
    return HttpResponse.json(listed(found));
  }),
  http.delete("/api/v1/orgs/:org/conversations/:id", ({ params }) => {
    conversations.delete(String(params.id));
    save();
    return new HttpResponse(null, { status: 204 });
  }),
  http.post("/api/v1/orgs/:org/conversations/:id/share", ({ params }) => {
    const found = conversations.get(String(params.id));
    if (!found) return notFound();
    found.token = crypto.randomUUID().replaceAll("-", "");
    save();
    return HttpResponse.json({ token: found.token, path: `/s/${found.token}` });
  }),
  http.delete("/api/v1/orgs/:org/conversations/:id/share", ({ params }) => {
    const found = conversations.get(String(params.id));
    if (found) found.token = null;
    save();
    return new HttpResponse(null, { status: 204 });
  }),
  http.post("/api/v1/orgs/:org/messages/:id/feedback", async ({ params, request }) => {
    const found = answerById(String(params.id));
    if (!found) return apiError(404, "message_not_found", "Answer not found.");
    const body = (await request.json()) as { kind: string; note?: string };
    found.feedback = {
      kind: body.kind,
      note: (body.note ?? "").trim(),
      created_at: new Date().toISOString(),
    };
    save();
    return HttpResponse.json(found.feedback);
  }),
  http.delete("/api/v1/orgs/:org/messages/:id/feedback", ({ params }) => {
    const found = answerById(String(params.id));
    if (found) found.feedback = null;
    save();
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("/api/v1/s/:token", ({ params }) => {
    const found = [...conversations.values()].find((c) => c.token === String(params.token));
    if (!found) return apiError(404, "share_not_found", "This link doesn't open anything.");
    const redact = mockScenario() === "signed-out";
    return HttpResponse.json({
      title: found.title,
      repo: redact ? null : "fastapi/full-stack-fastapi-template",
      redacted: redact,
      created_at: found.updated_at,
      updated_at: found.updated_at,
      messages: messagesOf(found, redact),
    });
  }),
];
