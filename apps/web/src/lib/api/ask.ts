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
  /** On a shared page, for a viewer who can't read the repository: only `n` and `span` are
   * kept (the rest is absent). */
  redacted?: boolean;
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

/** What the user was looking at when asking (the Ask bar's context): a file, its lines. */
export type ScopeLayer = "ui" | "api" | "svc" | "db";
export const SCOPE_LAYERS: readonly ScopeLayer[] = ["ui", "api", "svc", "db"];

/** What a question is about: a file (its lines) or a directory, layers, or both. */
export interface ScopeData {
  path: string | null;
  startLine: number | null;
  endLine: number | null;
  layers?: ScopeLayer[] | undefined; // none on questions asked before layers could be picked
}

/** A scope with nothing in it is none. */
export function scopeOrNull(scope: ScopeData): ScopeData | null {
  return scope.path === null && !scope.layers?.length ? null : scope;
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
  scope: ScopeData;
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

export function scopeOf(message: AskMessage): ScopeData | null {
  for (const part of message.parts) {
    const found = data(part, "scope");
    if (found !== null) return found;
  }
  return null;
}

export const LAYER_LABELS: Record<ScopeLayer, string> = {
  ui: "UI",
  api: "API",
  svc: "Service",
  db: "Data",
};

/** Where a scope's path points: `models.py:12–20` (a line: `models.py:12`; the file alone:
 * `models.py`; a directory: `app/`). */
export function pathLabel(scope: ScopeData): string | null {
  if (scope.path === null) return null;
  const name = scope.path.slice(scope.path.lastIndexOf("/") + 1);
  if (scope.startLine === null) return name.includes(".") ? name : `${name}/`;
  return chipLabel(name, [scope.startLine, scope.endLine ?? scope.startLine]);
}

/** The whole scope in a few words: `items.py:62–72 · API · Data`. */
export function scopeLabel(scope: ScopeData): string {
  const layers = (scope.layers ?? []).map((layer) => LAYER_LABELS[layer]);
  return [pathLabel(scope), ...layers].filter(Boolean).join(" · ");
}

/** The run that wrote a streamed answer (a stored one's comes with the conversation). */
export function runOf(message: AskMessage): string | null {
  for (const part of message.parts) {
    const found = data(part, "conversation");
    if (found !== null) return found.runId;
  }
  return null;
}

export function fromCache(message: AskMessage): boolean {
  return message.parts.some((part) => part.type === "data-cache");
}

/** A conversation's title, or `fallback` while it has none (not yet titled, or not loaded). */
export function titleOr(title: string | undefined, fallback: string): string {
  return title === undefined || title === "" ? fallback : title;
}

/** The answer's text: its text parts, in order. */
/** The answer's text: its last text part. An answer searched again for what it missed (or
 * rewritten to what its citations show) is sent again whole, after the one it replaces, and its
 * citations are where they fall in it. */
export function answerText(message: AskMessage): string {
  const texts = message.parts.filter((part) => part.type === "text");
  return texts.at(-1)?.text ?? "";
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
  const at = utf16Offsets(text);
  let out = text;
  for (const citation of ordered) {
    const point = Math.min(Math.max(citation.span[1], 0), at.length - 1);
    const end = at[point] ?? text.length;
    out = `${out.slice(0, end)}[${citation.n}](#cite-${citation.n})${out.slice(end)}`;
  }
  return out;
}

/**
 * Where each code point of `text` starts in it as JavaScript counts (UTF-16 units), and its
 * end last: the API's spans count code points (an emoji is one, here two).
 */
function utf16Offsets(text: string): number[] {
  const offsets: number[] = [];
  let unit = 0;
  for (const point of text) {
    offsets.push(unit);
    unit += point.length;
  }
  offsets.push(unit);
  return offsets;
}

/** A link the answer may open: the web's, mail, or within the page (not `javascript:`). */
export function safeHref(href: string | undefined): string | null {
  if (!href) return null;
  if (href.startsWith("#") || (href.startsWith("/") && !href.startsWith("//"))) return href;
  try {
    const url = new URL(href);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

export const VERBS: Record<string, string> = {
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

// ------------------------------------------------------------------ confidence, feedback

const s = (n: number) => (n === 1 ? "" : "s");

/** How sure the answer is, line by line: "2/2 claims verified against the code". */
export function confidenceLines(breakdown: ConfidenceData["breakdown"]): string[] {
  const b = breakdown;
  const lines: string[] = [];
  if (b.claims_total) {
    lines.push(`${b.claims_verified}/${b.claims_total} claims verified against the code`);
    if (b.claims_partial) lines.push(`${b.claims_partial} partly supported`);
    const uncited = b.claims_total - b.claims_cited;
    if (uncited > 0) lines.push(`${uncited} without a citation`);
  } else {
    lines.push("No claims to check");
  }
  if (b.inferred) lines.push(`${b.inferred} inferred from naming, not followed in the code`);
  if (b.unresolved_hops) lines.push(`${b.unresolved_hops} step${s(b.unresolved_hops)} not traced`);
  if (b.unknown_identifiers) {
    lines.push(`${b.unknown_identifiers} name${s(b.unknown_identifiers)} not found in the code`);
  }
  if (b.dropped_citations) {
    lines.push(`${b.dropped_citations} citation${s(b.dropped_citations)} dropped as unsupported`);
  }
  if (b.agreement !== null) lines.push(`Two searches agreed on ${Math.round(b.agreement * 100)}%`);
  if (b.whole === false) lines.push("Parts of the question may be unanswered");
  return lines;
}

export const FEEDBACK_KINDS = [
  { kind: "wrong_file", label: "Wrong file" },
  { kind: "outdated", label: "Outdated" },
  { kind: "missing_context", label: "Missing context" },
  { kind: "invented_symbol", label: "Invented symbol" },
  { kind: "other", label: "Other" },
] as const;

export type FeedbackKind = "helpful" | (typeof FEEDBACK_KINDS)[number]["kind"];

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

/** The run logs a query cut to this many characters (sensei_engine.agent.loop._searched). */
export const LOGGED_QUERY_MAX = 80;

const ESCAPES: Record<string, string> = {
  "\\": "\\",
  "'": "'",
  '"': '"',
  n: "\n",
  t: "\t",
  r: "\r",
};

/** A logged search (``grep "items/{id}"``, as the run lists them: Python's quoting): its tool
 * and its query as it was asked, for running it again on the search page. Null for a call that
 * isn't a search, or a query the log may have cut short (it wouldn't be the same search). */
export function searchOf(logged: string): { tool: "code_search" | "grep"; query: string } | null {
  const found = /^(code_search|grep) (['"])(.*)\2$/s.exec(logged);
  if (!found?.[1] || found[3] === undefined) return null;
  const escape = /\\(x[0-9a-f]{2}|u[0-9a-f]{4}|.)/gs;
  const codes = Array.from(found[3].matchAll(escape), (m) => m[1] ?? "");
  // An escape it doesn't know: not a query it can be sure of.
  if (codes.some((code) => code.length === 1 && ESCAPES[code] === undefined)) return null;
  const query = found[3].replace(escape, (_all, code: string) =>
    code.length > 1 ? String.fromCharCode(parseInt(code.slice(1), 16)) : (ESCAPES[code] ?? code),
  );
  // Python cut it at this many code points: one that long may not be the whole query.
  if (Array.from(query).length >= LOGGED_QUERY_MAX) return null;
  return { tool: found[1] as "code_search" | "grep", query };
}
