/**
 * Ask (4.10): the agent's answer as the AI SDK's UI messages, and what the page reads from them.
 *
 * `POST /api/v1/orgs/{org}/repos/{repo}/ask` streams the UI Message Stream (protocol v1): the
 * answer's text, each tool call, and `data-*` parts for its citations (each a span of the text
 * and the lines it cites), the run's progress, its confidence, what it searched, an abstention
 * and a cache hit. `useChat` assembles them into messages; the helpers here turn those into the
 * page's pieces: the citation chips in the text, the status line, the evidence rail.
 */
import { DefaultChatTransport, type UIMessage } from "ai";

import { readCsrfCookie } from "./client";

export type AskMode = "fast" | "deep";

export interface CitationData {
  n: number;
  repo: string;
  sha: string;
  path: string;
  lines: [number, number];
  symbolKey: string | null;
  span: [number, number];
  provenance: string;
  verified: boolean;
}

export interface ConfidenceData {
  level: "high" | "medium" | "low";
  score: number;
  breakdown: {
    claims_total: number;
    claims_cited: number;
    claims_verified: number;
    claims_partial: number;
    inferred: number;
    unresolved_hops: number;
    agreement: number | null;
    unknown_identifiers: number;
    dropped_citations: number;
    whole: boolean | null;
  };
}

export interface AbstainData {
  reason: string;
  searched: string[];
  trailEnd: { end: string | null; reason: string | null } | null;
  suggestion: string;
}

// A type, not an interface: the AI SDK's data types want an index signature.
// eslint-disable-next-line @typescript-eslint/consistent-type-definitions
export type AskData = {
  citation: CitationData;
  confidence: ConfidenceData;
  searchlog: { queries: string[] };
  progress: Record<string, unknown>;
  abstain: AbstainData;
  cache: { from: string };
  conversation: { id: string; runId: string | null };
};

export type AskMessage = UIMessage<unknown, AskData>;
type Part = AskMessage["parts"][number];

export function askEndpoint(org: string, repo: string): string {
  return `/api/v1/orgs/${encodeURIComponent(org)}/repos/${encodeURIComponent(repo)}/ask`;
}

/** The last user message's text: what a turn asks. */
export function lastQuestion(messages: readonly AskMessage[]): string {
  const asked = [...messages].reverse().find((message) => message.role === "user");
  if (asked === undefined) return "";
  return asked.parts.map((part) => (part.type === "text" ? part.text : "")).join("");
}

/**
 * The transport `useChat` sends a question through: the API takes the question alone (it keeps
 * the conversation), with the conversation it belongs to and the mode (given with each message:
 * `sendMessage({ text }, { body: { conversationId, mode } })`); the CSRF header the API wants on
 * a POST; the session's cookies.
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
    prepareSendMessagesRequest: ({ messages, body }) => ({
      body: {
        message: lastQuestion(messages),
        conversation_id: (body?.conversationId as string | null | undefined) ?? null,
        mode: (body?.mode as AskMode | undefined) ?? "fast",
      },
    }),
  });
}

// ------------------------------------------------------------------ reading a message

function data<K extends keyof AskData>(part: Part, kind: K): AskData[K] | null {
  if (part.type !== `data-${kind}`) return null;
  return (part as unknown as { data: AskData[K] }).data;
}

export function citationsOf(message: AskMessage): CitationData[] {
  const found = message.parts
    .map((part) => data(part, "citation"))
    .filter((c): c is CitationData => c !== null);
  return found.sort((a, b) => a.n - b.n);
}

export function confidenceOf(message: AskMessage): ConfidenceData | null {
  for (const part of message.parts) {
    const found = data(part, "confidence");
    if (found !== null) return found;
  }
  return null;
}

export function searchesOf(message: AskMessage): string[] {
  for (const part of message.parts) {
    const found = data(part, "searchlog");
    if (found !== null) return found.queries;
  }
  return [];
}

export function abstainedOf(message: AskMessage): AbstainData | null {
  for (const part of message.parts) {
    const found = data(part, "abstain");
    if (found !== null) return found;
  }
  return null;
}

export function fromCache(message: AskMessage): boolean {
  return message.parts.some((part) => part.type === "data-cache");
}

/** The answer's text: its text parts, in order. */
export function answerText(message: AskMessage): string {
  return message.parts.map((part) => (part.type === "text" ? part.text : "")).join("");
}

/** `file.tsx:12–48` (one line: `file.tsx:12`). */
export function chipLabel(path: string, [start, end]: readonly [number, number]): string {
  const name = path.slice(path.lastIndexOf("/") + 1);
  return start === end ? `${name}:${start}` : `${name}:${start}–${end}`;
}

/**
 * The answer's text with each citation's marker after the span it backs (a link to `#cite-n`,
 * which the renderer draws as its chip). Citations of one span share its end; markers go in
 * from the end so each offset stays right.
 */
export function withMarkers(text: string, citations: readonly CitationData[]): string {
  const ordered = [...citations].sort((a, b) => b.span[1] - a.span[1] || b.n - a.n);
  let out = text;
  for (const citation of ordered) {
    const at = Math.min(Math.max(citation.span[1], 0), out.length);
    out = `${out.slice(0, at)}[${citation.n}](#cite-${citation.n})${out.slice(at)}`;
  }
  return out;
}

const VERBS: Record<string, string> = {
  code_search: "Searching code",
  grep: "Searching text",
  find_symbol: "Finding symbols",
  graph_neighbors: "Following the graph",
  trace_feature: "Tracing the flow",
  read_code: "Reading files",
  list_dir: "Listing directories",
  get_feature_map: "Reading feature maps",
  submit_evidence: "Committing evidence",
};

export interface Activity {
  label: string;
  count: number;
}

/** What the run has done, by tool, in the order it first did each: "Reading files ×6". */
export function activity(message: AskMessage): Activity[] {
  const counts = new Map<string, number>();
  for (const part of message.parts) {
    if (!part.type.startsWith("tool-")) continue;
    const name = part.type.slice("tool-".length);
    const label = VERBS[name] ?? name;
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts].map(([label, count]) => ({ label, count }));
}

/** The status line while streaming: `Searching code · Reading files ×6 · Verifying`. */
export function statusLine(message: AskMessage | undefined, streaming: boolean): string {
  if (message === undefined) return streaming ? "Thinking" : "";
  const steps = activity(message).map((a) => (a.count > 1 ? `${a.label} ×${a.count}` : a.label));
  const verifying = message.parts.some(
    (part) => part.type === "data-progress" && part.id === "verified",
  );
  if (streaming && !verifying && answerText(message)) steps.push("Writing the answer");
  if (verifying) steps.push("Verified");
  return steps.join(" · ");
}

/** The sources a message cites, by file (in the order first cited), each file's ranges. */
export function sourcesByFile(message: AskMessage): { path: string; citations: CitationData[] }[] {
  const files = new Map<string, CitationData[]>();
  for (const citation of citationsOf(message)) {
    const held = files.get(citation.path) ?? [];
    held.push(citation);
    files.set(citation.path, held);
  }
  return [...files].map(([path, citations]) => ({ path, citations }));
}

// ------------------------------------------------------------------ drafts

const DRAFT_PREFIX = "sensei.ask.draft:";

export function draftKey(org: string, repo: string, conversationId: string | null): string {
  return `${DRAFT_PREFIX}${org}/${repo}/${conversationId ?? "new"}`;
}

export function readDraft(key: string): string {
  if (typeof localStorage === "undefined") return "";
  return localStorage.getItem(key) ?? "";
}

export function keepDraft(key: string, text: string): void {
  if (typeof localStorage === "undefined") return;
  if (text) localStorage.setItem(key, text);
  else localStorage.removeItem(key);
}
