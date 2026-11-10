/**
 * The transports `useChat` sends a question through (4.10; a public page's, 10.2), apart from
 * `ask` and `explore` so that reading an answer's pieces or a public repository doesn't load
 * the AI SDK's runtime (and zod with it).
 */
import { DefaultChatTransport } from "ai";

import { askEndpoint, lastQuestion, type AskMessage, type AskMode, type ScopeData } from "./ask";
import { fetchAfterMocks, readCsrfCookie } from "./client";
import { publicAskEndpoint } from "./explore";

/**
 * The transport `useChat` sends a question through: the API takes the question alone (it keeps
 * the conversation), with the conversation it belongs to, the mode and the ref the page is read
 * at (given with each message: `sendMessage({ text }, { body: { conversationId, mode, ref } })`);
 * the CSRF header the API wants on a POST; the session's cookies.
 */
export function askTransport(options: {
  org: string;
  repo: string;
  getCsrfToken?: () => string | null;
}): DefaultChatTransport<AskMessage> {
  const csrf =
    options.getCsrfToken ??
    (() => (typeof document === "undefined" ? null : readCsrfCookie(document.cookie)));
  return new DefaultChatTransport<AskMessage>({
    api: askEndpoint(options.org, options.repo),
    credentials: "include",
    headers: () => {
      const token = csrf();
      const headers: Record<string, string> = {};
      if (token) headers["X-CSRF-Token"] = token;
      return headers;
    },
    prepareSendMessagesRequest: ({ messages, body }) => {
      const scope = body?.scope as ScopeData | null | undefined;
      const ref = body?.ref as string | null | undefined;
      return {
        body: {
          message: lastQuestion(messages),
          conversation_id: (body?.conversationId as string | null | undefined) ?? null,
          mode: (body?.mode as AskMode | undefined) ?? "fast",
          ...(ref ? { ref } : {}),
          ...(scope
            ? {
                scope: {
                  path: scope.path,
                  start_line: scope.startLine,
                  end_line: scope.endLine,
                  layers: scope.layers ?? [],
                },
              }
            : {}),
        },
      };
    },
  });
}

/**
 * The transport a public page's question goes through: the question alone (a first question,
 * every time) and the Turnstile token given with it (`sendMessage({ text }, { body: { token }
 * })`). No cookies: whoever asks, it is asked as no one.
 */
export function publicAskTransport(owner: string, name: string): DefaultChatTransport<AskMessage> {
  return new DefaultChatTransport<AskMessage>({
    api: publicAskEndpoint(owner, name),
    credentials: "omit",
    // In mock mode, after the mock API is up (a public page asks nothing of it before this).
    fetch: (input, init) => fetchAfterMocks(new Request(input, init)),
    prepareSendMessagesRequest: ({ messages, body }) => ({
      body: {
        message: lastQuestion(messages),
        turnstile_token: (body?.token as string | undefined) ?? "",
      },
    }),
  });
}
