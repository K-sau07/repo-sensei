/**
 * The public gallery (10.2): the repositories the explore org publishes, read by anyone at
 * their last index (`GET /api/v1/public/repos…`), and the question anyone may ask about one
 * (`POST …/ask`, behind a Cloudflare Turnstile check). The helpers here are what its pages draw
 * from that: where a symbol's lines are on GitHub, at the commit the page was verified at.
 */
import { DefaultChatTransport } from "ai";

import { lastQuestion, type AskMessage } from "./ask";
import { fetchAfterMocks } from "./client";
import type { Schemas } from "./types";

export type PublicRepo = Schemas["PublicRepoOut"];
export type PublicRepoPage = Schemas["PublicRepoPage"];
export type FeatureMap = Schemas["FeatureMapOut"];
export type FeatureMapList = Schemas["FeatureMapList"];

/** What Cloudflare's test site keys hand the page instead of a real token. */
export const DUMMY_TOKEN = "XXXX.DUMMY.TOKEN.XXXX";

/** An entry point or key symbol of a feature map, with its lines. */
export interface MapSymbol {
  symbolKey: string;
  name: string;
  why: string;
  path: string;
  lines: [number, number];
}

/** A map's entry points or key symbols, as stored ({symbol_key, why, path, start_line,
 * end_line}); any without a file and lines is left out (it has nowhere to point). */
export function mapSymbols(items: readonly Record<string, unknown>[]): MapSymbol[] {
  const found: MapSymbol[] = [];
  for (const item of items) {
    const { symbol_key: key, why, path, start_line: start, end_line: end } = item;
    if (typeof key !== "string" || typeof path !== "string" || !path) continue;
    if (typeof start !== "number" || typeof end !== "number" || start < 1 || end < start) continue;
    found.push({
      symbolKey: key,
      name: symbolName(key),
      why: typeof why === "string" ? why : "",
      path,
      lines: [start, end],
    });
  }
  return found;
}

/** A symbol key's readable name: ``py a/b.py/Item#save().`` → ``Item.save``. */
export function symbolName(key: string): string {
  const rest = key.includes(" ") ? key.slice(key.indexOf(" ") + 1) : key;
  const tail = rest.slice(rest.lastIndexOf("/") + 1) || rest;
  const parts = tail.replaceAll("().", ".").replaceAll("#", ".").split(".").filter(Boolean);
  return parts.join(".") || tail;
}

/** The file on GitHub at ``sha``, its lines marked (``#L3-L9``). Each path segment is encoded,
 * so a name with ``#`` or ``?`` in it still opens its file. */
export function githubBlob(
  fullName: string,
  sha: string,
  path: string,
  lines?: readonly [number, number],
): string {
  const file = path.split("/").map(encodeURIComponent).join("/");
  const marked = lines
    ? lines[0] === lines[1]
      ? `#L${lines[0]}`
      : `#L${lines[0]}-L${lines[1]}`
    : "";
  return `https://github.com/${fullName}/blob/${sha}/${file}${marked}`;
}

/** A directory on GitHub at ``sha`` (the repository's top for ``"."``). */
export function githubTree(fullName: string, sha: string, path: string): string {
  const dir = path === "." || !path ? "" : `/${path.split("/").map(encodeURIComponent).join("/")}`;
  return `https://github.com/${fullName}/tree/${sha}${dir}`;
}

export function githubCommit(fullName: string, sha: string): string {
  return `https://github.com/${fullName}/commit/${sha}`;
}

export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}

/** Its languages by share of its code, the largest first (at most ``top``; the rest are
 * "Other"). */
export function languageShares(
  languages: Readonly<Record<string, number>>,
  top = 4,
): { language: string; share: number }[] {
  const total = Object.values(languages).reduce((sum, n) => sum + n, 0);
  if (total <= 0) return [];
  const sorted = Object.entries(languages)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const shown = sorted.slice(0, top).map(([language, n]) => ({ language, share: n / total }));
  const rest = sorted.slice(top).reduce((sum, [, n]) => sum + n, 0);
  return rest > 0 ? [...shown, { language: "Other", share: rest / total }] : shown;
}

export function publicAskEndpoint(owner: string, name: string): string {
  return `/api/v1/public/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/ask`;
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

/** What a refused question says: the transport's error carries the API's body as its message
 * (`{"error": {"message": …}}`); anything else is said plainly. */
export function askFailure(error: Error): string {
  try {
    const body = JSON.parse(error.message) as { error?: { message?: unknown } };
    if (typeof body.error?.message === "string") return body.error.message;
  } catch {
    // not the API's answer: a network failure, a stream cut short
  }
  return "The answer couldn't be written. Try again in a moment.";
}
