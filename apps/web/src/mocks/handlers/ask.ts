/**
 * The mock `POST …/ask` (the answer streamed as the UI Message Stream, a part every few
 * milliseconds so it is seen streaming), the user's conversations and one conversation, kept
 * in memory as the page asks.
 *
 * Situations worth seeing, from the browser:
 *   localStorage.setItem("sensei.mock.ask", "slow")
 * "slow" (150 ms a part).
 */
import { delay, http, HttpResponse } from "msw";

import { mockAnswerParts } from "@/mocks/ask";

const ASK_SCENARIO_KEY = "sensei.mock.ask";
const PART_MS = 15;
const SLOW_MS = 150;

interface Stored {
  id: string;
  repo: string;
  title: string;
  messages: { id: string; role: "user" | "assistant"; parts: Record<string, unknown>[] }[];
  updated_at: string;
}

const conversations = new Map<string, Stored>();

function slow(): boolean {
  return typeof localStorage !== "undefined" && localStorage.getItem(ASK_SCENARIO_KEY) === "slow";
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
    const parts = mockAnswerParts(id);
    const held = conversations.get(id) ?? {
      id,
      repo: String(params.repo),
      title: question.slice(0, 80),
      messages: [],
      updated_at: new Date().toISOString(),
    };
    held.messages.push(
      { id: crypto.randomUUID(), role: "user", parts: [{ type: "text", text: question }] },
      { id: messageId, role: "assistant", parts: assembled(parts) },
    );
    held.updated_at = new Date().toISOString();
    conversations.set(id, held);
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
  http.get("/api/v1/orgs/:org/repos/:repo/conversations", ({ params }) =>
    HttpResponse.json({
      conversations: [...conversations.values()]
        .filter((c) => c.repo === params.repo)
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .map(({ id, title, updated_at }) => ({
          id,
          repo_id: "00000000-0000-0000-0000-000000000000",
          title,
          created_at: updated_at,
          updated_at,
        })),
      next_cursor: null,
    }),
  ),
  http.get("/api/v1/orgs/:org/conversations/:id", ({ params }) => {
    const found = conversations.get(String(params.id));
    if (!found) {
      return HttpResponse.json(
        { error: { code: "conversation_not_found", message: "Conversation not found." } },
        { status: 404 },
      );
    }
    return HttpResponse.json({
      id: found.id,
      repo_id: "00000000-0000-0000-0000-000000000000",
      title: found.title,
      created_at: found.updated_at,
      updated_at: found.updated_at,
      messages: found.messages.map((m) => ({
        ...m,
        status: "done",
        confidence: null,
        abstained: false,
        created_at: found.updated_at,
        citations: [],
      })),
    });
  }),
];
