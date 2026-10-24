/**
 * The mock `POST …/search` and `POST …/grep`: a small but honest search over the files whose
 * content the mock serves (fixtures/browse-content.json), so every hit opens on lines that exist.
 *
 * - The query is read by a few rules: quotes or backticks are literals, regex metacharacters make
 *   a regex, one identifier is a symbol, anything else a concept.
 * - The keyword leg finds the query's words in the lines (case-insensitively, literals exactly),
 *   weighted by how rare each word is across the files; the symbol leg is the symbols endpoint's
 *   own lookup (mocks/symbols.ts). A small table of related words stands in for embeddings (the
 *   semantic leg), which partial mode leaves out.
 * - A match inside a symbol is a hit on the whole symbol; elsewhere (markdown, config) on the
 *   lines around it. Each file's layer comes from its role, as the constellation's does.
 */
import {
  LAYERS,
  SEARCH_LIMIT_DEFAULT,
  SEARCH_LIMIT_MAX,
  SEARCH_QUERY_MAX,
  SNIPPET_LINES_MAX,
  type ExactQuery,
  type Interpretation,
  type Layer,
  type MatchReason,
  type PerLayer,
  type SearchHit,
  type SearchMode,
  type SearchRequest,
  type SearchResponse,
  type SnippetHighlight,
} from "@/lib/api/search";
import type { GrepHit, GrepRequest, GrepResult } from "@/lib/api/types";

import { fileAt, type MockFile } from "./browse";
import { SYMBOLS, searchSymbols, type MockSymbol } from "./symbols";

// ----------------------------------------------------------------- files

const ROLE_LAYERS: Record<string, Layer> = { ui: "ui", api: "api", service: "svc", db: "db" };

/** The constellation's layer for a file's role: tests, config, docs and the rest are "other". */
export const layerOfRole = (role: string): Layer => ROLE_LAYERS[role] ?? "other";

export interface Indexed {
  file: MockFile;
  layer: Layer;
  lines: readonly string[];
  /** The file's symbols, other than the module itself, innermost last. */
  symbols: readonly MockSymbol[];
}

/** A file's lines as the code browser numbers them (a final newline ends the last line). */
export function linesOf(text: string): string[] {
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines.map((line) => (line.endsWith("\r") ? line.slice(0, -1) : line));
}

/** The searchable files: those the fixture has real text for, in path order. */
export function indexFiles(content: Readonly<Record<string, string>>): Indexed[] {
  const out: Indexed[] = [];
  for (const [path, text] of Object.entries(content)) {
    const file = fileAt(path);
    if (!file || file.flags.includes("binary")) continue;
    out.push({
      file,
      layer: layerOfRole(file.role),
      lines: linesOf(text),
      symbols: SYMBOLS.filter((symbol) => symbol.path === path && symbol.kind !== "module"),
    });
  }
  return out.sort((a, b) => (a.file.path < b.file.path ? -1 : a.file.path > b.file.path ? 1 : 0));
}

// ----------------------------------------------------------------- reading the query

const QUOTED = /"([^"]+)"|'([^']+)'|`([^`]+)`/g;
const REGEX_HINT =
  /\\[wdsbWDSB.()[\]{}|^$*+?]|\.\*|\.\+|\[[^\]\s]+\]|^\^|\$$|\([^)\s]*\|[^)]*\)|\{\d+(?:,\d*)?\}/;
const IDENTIFIER = /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$/;
const PATH_LIKE = /^[\w.-]+\/[\w./-]*$/;
const CODE_LIKE = /[_$.]|[a-z][A-Z]/;
const STOPWORDS = new Set(
  (
    "a an and are as at be by do does done for from how i in is it its me my of on or our so " +
    "that the their them then there these this to us was we what when where which who why " +
    "will with you your get gets got find show all any can"
  ).split(" "),
);

/** A small table of related words, standing in for what embeddings would find. */
const RELATED: Record<string, readonly string[]> = {
  login: ["auth", "token", "password"],
  auth: ["login", "token", "oauth2"],
  password: ["hash", "secret", "verify"],
  token: ["jwt", "access", "auth"],
  user: ["account", "superuser"],
  users: ["account", "superuser"],
  item: ["items", "owner"],
  create: ["add", "new", "post"],
  delete: ["remove", "cascade"],
  update: ["patch", "put", "edit"],
  email: ["mail", "smtp"],
  database: ["db", "session", "engine"],
  db: ["database", "session", "engine"],
  migration: ["alembic", "revision", "upgrade"],
  config: ["settings", "env"],
  settings: ["config", "env"],
  route: ["router", "endpoint", "path"],
  endpoint: ["router", "route"],
  table: ["column", "model"],
  model: ["table", "sqlmodel"],
  test: ["pytest", "client", "assert"],
  sidebar: ["menu", "nav"],
  form: ["input", "submit"],
};

export interface Term {
  text: string;
  /** Literals match exactly; words match in any case. */
  exact: boolean;
  leg: "keyword" | "semantic";
}

export interface Reading {
  interpretation: Interpretation;
  terms: Term[];
  /** A regex query's pattern, compiled. */
  regex: RegExp | null;
  /** The name the symbol leg looks up. */
  symbolQueries: string[];
  exact: ExactQuery[];
}

export class InvalidQuery extends Error {}

const singular = (word: string) =>
  word.endsWith("ies") && word.length > 4
    ? `${word.slice(0, -3)}y`
    : word.endsWith("s") && !word.endsWith("ss") && word.length > 3
      ? word.slice(0, -1)
      : word;

const unique = <T>(values: readonly T[]): T[] => [...new Set(values)];

/** How the query is read: its class, what it searches for, and the exact queries behind it. */
export function readQuery(raw: string): Reading {
  const q = raw.trim();
  const quoted = [...q.matchAll(QUOTED)].map((match) => match[1] ?? match[2] ?? match[3] ?? "");
  if (quoted.length > 0) {
    const literals = unique(quoted.filter(Boolean));
    return {
      interpretation: { class: "literal", rewrites: [], literals, path_filter: null },
      terms: literals.map((text) => ({ text, exact: true, leg: "keyword" })),
      regex: null,
      symbolQueries: literals.filter((literal) => IDENTIFIER.test(literal)),
      exact: literals.map((query) => ({ kind: "literal", query })),
    };
  }
  if (REGEX_HINT.test(q)) {
    let regex: RegExp;
    try {
      regex = new RegExp(q, /[A-Z]/.test(q.replace(/\\./g, "")) ? "g" : "gi");
    } catch {
      throw new InvalidQuery("That isn't a regular expression we can run.");
    }
    return {
      interpretation: { class: "regex", rewrites: [], literals: [], path_filter: null },
      terms: [],
      regex,
      symbolQueries: [],
      exact: [{ kind: "regex", query: q }],
    };
  }
  if (IDENTIFIER.test(q)) {
    const name = q.slice(q.lastIndexOf(".") + 1);
    const definition = `\\b(def|class|function|const|let|interface|type)\\s+${name}\\b`;
    return {
      interpretation: { class: "symbol", rewrites: [], literals: [q], path_filter: null },
      terms: [{ text: name, exact: false, leg: "keyword" }],
      regex: null,
      symbolQueries: [q],
      exact: [
        { kind: "symbol", query: q },
        { kind: "literal", query: name },
        { kind: "regex", query: definition },
      ],
    };
  }

  const tokens = q.split(/[\s,;:!?()"'`]+/).filter(Boolean);
  const pathToken = tokens.find((token) => PATH_LIKE.test(token)) ?? null;
  const pathFilter = pathToken ? pathToken.replace(/^\/+/, "") : null;
  const codeTokens = tokens.filter((token) => token !== pathToken && CODE_LIKE.test(token));
  const words = unique(
    tokens
      .filter((token) => token !== pathToken && !codeTokens.includes(token))
      .map((token) => token.toLowerCase().replace(/[^\p{L}\p{N}_-]/gu, ""))
      .filter((word) => word.length >= 2 && !STOPWORDS.has(word)),
  );
  const joined = words.slice(1).map((word, index) => `${words[index] ?? ""}_${word}`);
  const related = words.flatMap((word) => [
    ...(singular(word) !== word ? [singular(word)] : []),
    ...(RELATED[word] ?? RELATED[singular(word)] ?? []),
  ]);
  const rewrites = unique([...joined, ...related]).filter(
    (rewrite) => !words.includes(rewrite) && !codeTokens.includes(rewrite),
  );
  const literals = unique(codeTokens);
  const exact: ExactQuery[] = [
    ...literals.map((query): ExactQuery => ({ kind: "literal", query })),
    ...(words.length >= 2
      ? [{ kind: "regex", query: words.slice(0, 3).join(".*") } satisfies ExactQuery]
      : words.map((query): ExactQuery => ({ kind: "literal", query }))),
    ...(pathFilter ? [{ kind: "path", query: pathFilter } satisfies ExactQuery] : []),
  ];
  return {
    interpretation: { class: "conceptual", rewrites, literals, path_filter: pathFilter },
    terms: [
      ...literals.map((text): Term => ({ text, exact: true, leg: "keyword" })),
      ...words.map((text): Term => ({ text, exact: false, leg: "keyword" })),
      ...rewrites.map((text): Term => ({ text, exact: false, leg: "semantic" })),
    ],
    regex: null,
    symbolQueries: unique([...literals, ...joined]),
    exact: exact.slice(0, 5),
  };
}

// ----------------------------------------------------------------- matching

const escapeRegex = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** A term as a pattern: short words only as whole words (so "id" isn't in every line). */
function termPattern(term: Term): RegExp {
  const body = escapeRegex(term.text);
  if (term.exact) return new RegExp(body, "g");
  return new RegExp(term.text.length < 3 ? `\\b${body}\\b` : body, "gi");
}

interface Match {
  line: number;
  start: number;
  end: number;
  term: string;
  leg: "keyword" | "semantic";
}

/** Every match of `pattern` in `text`, as UTF-16 ranges (empty matches skipped). */
function matchesIn(text: string, pattern: RegExp): [number, number][] {
  const out: [number, number][] = [];
  pattern.lastIndex = 0;
  for (const found of text.matchAll(pattern)) {
    if (found[0].length === 0) continue;
    out.push([found.index, found.index + found[0].length]);
  }
  return out;
}

interface Draft {
  id: string;
  symbol: MockSymbol | null;
  indexed: Indexed;
  start: number;
  end: number;
  matches: Match[];
  symbolScore: number;
}

/** The innermost symbol around `line`. */
function symbolAt(indexed: Indexed, line: number): MockSymbol | null {
  let best: MockSymbol | null = null;
  for (const symbol of indexed.symbols) {
    if (symbol.start_line > line || symbol.end_line < line) continue;
    if (!best || symbol.end_line - symbol.start_line < best.end_line - best.start_line) {
      best = symbol;
    }
  }
  return best;
}

/** Matches outside any symbol, gathered into regions of at most 12 lines (with a line of
 * context either side when there's room). */
function regions(
  indexed: Indexed,
  loose: Match[],
): { start: number; end: number; matches: Match[] }[] {
  const out: { start: number; end: number; matches: Match[] }[] = [];
  const count = indexed.lines.length;
  for (const match of [...loose].sort((a, b) => a.line - b.line)) {
    const last = out.at(-1);
    const lastLine = last?.matches.at(-1)?.line ?? -Infinity;
    if (last && match.line - lastLine <= 3 && match.line - last.start < SNIPPET_LINES_MAX - 1) {
      last.matches.push(match);
      last.end = Math.min(
        count,
        Math.max(last.end, match.line + 1),
        last.start + SNIPPET_LINES_MAX - 1,
      );
    } else {
      out.push({
        start: Math.max(1, match.line - 1),
        end: Math.min(count, match.line + 1),
        matches: [match],
      });
    }
  }
  return out;
}

export interface Filters {
  layers: readonly Layer[] | null;
  langs: readonly string[] | null;
  path: string | null;
}

function passes(indexed: Indexed, { layers, langs, path }: Filters): boolean {
  if (layers && layers.length > 0 && !layers.includes(indexed.layer)) return false;
  if (langs && langs.length > 0 && !langs.includes(indexed.file.language ?? "")) return false;
  return !path || indexed.file.path.startsWith(path);
}

/** How rare each term is: ln(1 + files / files containing it). */
function rarity(files: readonly Indexed[], terms: readonly Term[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const term of terms) {
    const pattern = termPattern(term);
    let containing = 0;
    for (const indexed of files) {
      if (indexed.lines.some((line) => matchesIn(line, pattern).length > 0)) containing += 1;
    }
    out.set(term.text, Math.log(1 + files.length / Math.max(1, containing)));
  }
  return out;
}

const LEG_WEIGHT = { keyword: 0.75, semantic: 0.45, symbol: 0.8 } as const;
const round = (value: number) => Math.round(value * 10_000) / 10_000;

/** A hit's snippet: the whole span when it fits, else the 12 lines from just above its first
 * match; with every match in those lines. */
function snippetOf(draft: Draft, highlightName: boolean): SearchHit["snippet"] {
  const { start, end, indexed, matches } = draft;
  const first = matches.reduce((low, match) => Math.min(low, match.line), Infinity);
  let from = start;
  if (end - start + 1 > SNIPPET_LINES_MAX && Number.isFinite(first)) {
    from = Math.max(start, Math.min(first - 2, end - SNIPPET_LINES_MAX + 1));
  }
  const to = Math.min(end, from + SNIPPET_LINES_MAX - 1);
  const lines = indexed.lines.slice(from - 1, to);
  const highlights: SnippetHighlight[] = matches
    .filter((match) => match.line >= from && match.line <= to)
    .map(({ line, start: s, end: e }) => ({ line, start: s, end: e }));
  if (highlightName && draft.symbol) {
    const pattern = new RegExp(`\\b${escapeRegex(draft.symbol.name)}\\b`, "g");
    lines.some((text, index) => {
      const found = matchesIn(text, pattern)[0];
      if (found) highlights.push({ line: from + index, start: found[0], end: found[1] });
      return found !== undefined;
    });
  }
  highlights.sort((a, b) => a.line - b.line || a.start - b.start || a.end - b.end);
  const seen = new Set<string>();
  return {
    start_line: from,
    lines,
    highlights: highlights.filter((h) => {
      const key = `${String(h.line)}:${String(h.start)}:${String(h.end)}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }),
  };
}

/** Every hit for the reading, best first, with the filters applied. */
export function findHits(
  files: readonly Indexed[],
  reading: Reading,
  filters: Filters,
  { semantic, keyword }: { semantic: boolean; keyword: boolean },
): SearchHit[] {
  // Semantic mode reads every term by meaning; without embeddings, related words are left out.
  const terms = reading.terms.filter((term) => term.leg === "keyword" || semantic);
  const scan: { pattern: RegExp; term: string; leg: Match["leg"] }[] = terms.map((term) => ({
    pattern: termPattern(term),
    term: term.text,
    leg: keyword ? term.leg : "semantic",
  }));
  if (reading.regex && keyword) {
    scan.push({ pattern: reading.regex, term: reading.regex.source, leg: "keyword" });
  }
  const weights = rarity(files, terms);
  const drafts = new Map<string, Draft>();
  const shown = files.filter((indexed) => passes(indexed, filters));

  for (const indexed of shown) {
    const loose: Match[] = [];
    indexed.lines.forEach((text, index) => {
      const line = index + 1;
      for (const { pattern, term, leg } of scan) {
        for (const [start, end] of matchesIn(text, pattern)) {
          const match = { line, start, end, term, leg };
          const symbol = symbolAt(indexed, line);
          if (!symbol) {
            loose.push(match);
            continue;
          }
          const draft = drafts.get(symbol.symbol_key) ?? {
            id: symbol.symbol_key,
            symbol,
            indexed,
            start: symbol.start_line,
            end: symbol.end_line,
            matches: [],
            symbolScore: 0,
          };
          draft.matches.push(match);
          drafts.set(draft.id, draft);
        }
      }
    });
    for (const region of regions(indexed, loose)) {
      const id = `${indexed.file.path}#L${String(region.start)}-${String(region.end)}`;
      drafts.set(id, { id, symbol: null, indexed, ...region, symbolScore: 0 });
    }
  }

  // The symbol leg: the symbols endpoint's lookup, for what reads as a name.
  if (keyword) {
    for (const query of reading.symbolQueries) {
      for (const found of searchSymbols(query, { kind: null, limit: 20 })) {
        const indexed = shown.find((entry) => entry.file.path === found.path);
        const symbol = indexed?.symbols.find((entry) => entry.symbol_key === found.symbol_key);
        if (!indexed || !symbol) continue;
        const draft = drafts.get(symbol.symbol_key) ?? {
          id: symbol.symbol_key,
          symbol,
          indexed,
          start: symbol.start_line,
          end: symbol.end_line,
          matches: [],
          symbolScore: 0,
        };
        draft.symbolScore = Math.max(draft.symbolScore, found.score);
        drafts.set(draft.id, draft);
      }
    }
  }

  const raw = [...drafts.values()].map((draft) => {
    const legs = { keyword: 0, semantic: 0 };
    for (const leg of ["keyword", "semantic"] as const) {
      const counts = new Map<string, number>();
      for (const match of draft.matches) {
        if (match.leg === leg) counts.set(match.term, (counts.get(match.term) ?? 0) + 1);
      }
      for (const [term, count] of counts) {
        const weight = weights.get(term) ?? 1;
        const inName = draft.symbol?.name.toLowerCase().includes(term.toLowerCase()) ?? false;
        legs[leg] += (1 + Math.log(count)) * weight + (inName ? weight : 0);
      }
    }
    return { draft, legs };
  });
  const top = {
    keyword: Math.max(1e-9, ...raw.map((entry) => entry.legs.keyword)),
    semantic: Math.max(1e-9, ...raw.map((entry) => entry.legs.semantic)),
  };

  const hits = raw.map(({ draft, legs }): SearchHit => {
    const parts: [MatchReason, number][] = [
      ["keyword", (legs.keyword / top.keyword) * LEG_WEIGHT.keyword],
      ["semantic", (legs.semantic / top.semantic) * LEG_WEIGHT.semantic],
      ["symbol", draft.symbolScore * LEG_WEIGHT.symbol],
    ];
    const present = parts.filter(([, value]) => value > 0).sort((a, b) => b[1] - a[1]);
    const score = 1 - present.reduce((rest, [, value]) => rest * (1 - value), 1);
    const { symbol, indexed } = draft;
    const name = symbol?.name ?? indexed.file.path.slice(indexed.file.path.lastIndexOf("/") + 1);
    return {
      id: draft.id,
      symbol_key: symbol?.symbol_key ?? null,
      name,
      kind: symbol?.kind ?? "file",
      path: indexed.file.path,
      language: indexed.file.language,
      layer: indexed.layer,
      start_line: draft.start,
      end_line: draft.end,
      snippet: snippetOf(draft, draft.matches.length === 0),
      score: round(score),
      why: present.map(([reason]) => reason),
    };
  });
  return hits
    .filter((hit) => hit.why.length > 0)
    .sort(
      (a, b) =>
        b.score - a.score ||
        (a.path < b.path ? -1 : a.path > b.path ? 1 : 0) ||
        a.start_line - b.start_line,
    );
}

// ----------------------------------------------------------------- the endpoint

export class SearchError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly field?: string,
  ) {
    super(message);
  }
}

interface CursorState {
  v: 1;
  key: string;
  lane: Layer;
  offset: number;
  layers: Layer[] | null;
}

function encodeCursor(state: CursorState): string {
  return btoa(JSON.stringify(state)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodeCursor(cursor: string): CursorState | null {
  try {
    const state = JSON.parse(atob(cursor.replace(/-/g, "+").replace(/_/g, "/"))) as unknown;
    if (typeof state !== "object" || state === null) return null;
    const record = state as Partial<CursorState>;
    if (
      record.v !== 1 ||
      typeof record.key !== "string" ||
      record.lane === undefined ||
      !LAYERS.includes(record.lane) ||
      !Number.isInteger(record.offset) ||
      (record.offset ?? -1) < 0
    ) {
      return null;
    }
    return record as CursorState;
  } catch {
    return null;
  }
}

/** What a cursor belongs to: the same query, filters (other than the lane), mode and page size. */
function searchKey(body: SearchRequest, ref: string, limit: number): string {
  return JSON.stringify([
    body.q.trim(),
    [...(body.langs ?? [])].sort(),
    body.path ?? null,
    ref,
    body.mode ?? "auto",
    limit,
  ]);
}

const perLayer = <T>(make: (layer: Layer) => T): PerLayer<T> =>
  Object.fromEntries(LAYERS.map((layer) => [layer, make(layer)])) as PerLayer<T>;

/** The body's fields, checked as the API checks them. */
export function checkBody(body: unknown): SearchRequest {
  if (typeof body !== "object" || body === null) {
    throw new SearchError(422, "validation_failed", "Request validation failed.", "body");
  }
  const request = body as Partial<SearchRequest>;
  if (typeof request.q !== "string") {
    throw new SearchError(422, "validation_failed", "Field required", "q");
  }
  if (request.q.length < 1 || request.q.length > SEARCH_QUERY_MAX) {
    throw new SearchError(
      422,
      "validation_failed",
      `String should have 1 to ${String(SEARCH_QUERY_MAX)} characters`,
      "q",
    );
  }
  const limit = request.limit ?? SEARCH_LIMIT_DEFAULT;
  if (!Number.isInteger(limit) || limit < 1 || limit > SEARCH_LIMIT_MAX) {
    throw new SearchError(422, "validation_failed", "Input should be between 1 and 50", "limit");
  }
  if (request.layers?.some((layer) => !LAYERS.includes(layer))) {
    throw new SearchError(
      422,
      "validation_failed",
      `Input should be one of: ${LAYERS.join(", ")}`,
      "layers",
    );
  }
  const modes: SearchMode[] = ["auto", "keyword", "semantic"];
  if (request.mode !== undefined && !modes.includes(request.mode)) {
    throw new SearchError(
      422,
      "validation_failed",
      "Input should be auto, keyword or semantic",
      "mode",
    );
  }
  if (!request.q.trim()) {
    throw new SearchError(422, "invalid_query", "Type something to search for.");
  }
  return request as SearchRequest;
}

/** The search over `files` for a checked body. `semanticReady`: the snapshot's embeddings are
 * done (otherwise partial: keyword and symbol legs only). */
export function search(
  files: readonly Indexed[],
  body: SearchRequest,
  { semanticReady, commit }: { semanticReady: boolean; commit: string },
): SearchResponse {
  const mode = body.mode ?? "auto";
  if (mode === "semantic" && !semanticReady) {
    throw new SearchError(
      409,
      "semantic_not_ready",
      "Semantic search is available once this commit's embeddings are done.",
    );
  }
  const limit = body.limit ?? SEARCH_LIMIT_DEFAULT;
  const key = searchKey(body, commit, limit);
  let cursor: CursorState | null = null;
  if (body.cursor) {
    cursor = decodeCursor(body.cursor);
    if (cursor?.key !== key || body.layers?.length !== 1 || body.layers[0] !== cursor.lane) {
      throw new SearchError(422, "invalid_cursor", "That page of results is no longer available.");
    }
  }
  let reading: Reading;
  try {
    reading = readQuery(body.q);
  } catch (error) {
    if (error instanceof InvalidQuery) throw new SearchError(422, "invalid_query", error.message);
    throw error;
  }
  // The lanes the counts are for: the first page's filter, kept in the cursor.
  const layers = cursor ? cursor.layers : (body.layers ?? null);
  const prefix = body.path?.replace(/^\/+/, "") ?? "";
  const filters: Filters = {
    layers,
    langs: body.langs ?? null,
    path: prefix !== "" ? prefix : reading.interpretation.path_filter,
  };
  const legs = {
    keyword: mode !== "semantic",
    semantic: mode !== "keyword" && semanticReady,
  };
  const hits = findHits(files, reading, filters, legs);
  const byLane = perLayer((layer) => hits.filter((hit) => hit.layer === layer));
  const offsetOf = (layer: Layer) => (cursor?.lane === layer ? cursor.offset : 0);
  const shownLane = (layer: Layer) => !cursor || cursor.lane === layer;
  return {
    query: body.q.trim(),
    interpretation: reading.interpretation,
    lanes: perLayer((layer) =>
      shownLane(layer) ? byLane[layer].slice(offsetOf(layer), offsetOf(layer) + limit) : [],
    ),
    counts: perLayer((layer) => byLane[layer].length),
    next: perLayer((layer) => {
      const after = offsetOf(layer) + limit;
      if (!shownLane(layer) || after >= byLane[layer].length) return null;
      return encodeCursor({ v: 1, key, lane: layer, offset: after, layers });
    }),
    exact_queries: reading.exact,
    partial: { semantic_pending: !semanticReady, summaries_pending: !semanticReady },
  };
}

// ----------------------------------------------------------------- exact search (grep)

const GREP_LIMIT_DEFAULT = 50;
const GREP_LIMIT_MAX = 500;
const GREP_CONTEXT_MAX = 10;
const MAX_LINE_CHARS = 500;
const MAX_RANGES = 50;

/** A ripgrep glob as a test on a path: `**` spans directories, `*` and `?` don't, and a glob
 * with no slash matches a file name anywhere. */
export function globTest(glob: string): (path: string) => boolean {
  let body = "";
  for (let i = 0; i < glob.length; i += 1) {
    const char = glob.charAt(i);
    if (char === "*" && glob.charAt(i + 1) === "*") {
      body += ".*";
      i += 1;
      if (glob.charAt(i + 1) === "/") i += 1;
    } else if (char === "*") body += "[^/]*";
    else if (char === "?") body += "[^/]";
    else body += escapeRegex(char);
  }
  const pattern = new RegExp(glob.includes("/") ? `^${body}$` : `(^|/)${body}$`);
  return (path) => pattern.test(path);
}

/** Code point offsets for UTF-16 ones in `text`. */
function codePointAt(text: string, utf16: number): number {
  return Array.from(text.slice(0, utf16)).length;
}

export function grep(files: readonly Indexed[], body: unknown, commit: string): GrepResult {
  if (typeof body !== "object" || body === null) {
    throw new SearchError(422, "validation_failed", "Request validation failed.", "body");
  }
  const request = body as Partial<GrepRequest>;
  const pattern = request.pattern;
  if (typeof pattern !== "string" || pattern.length < 1 || pattern.length > 1000) {
    throw new SearchError(
      422,
      "validation_failed",
      "String should have 1 to 1000 characters",
      "pattern",
    );
  }
  const limit = request.limit ?? GREP_LIMIT_DEFAULT;
  const context = request.context ?? 0;
  if (!Number.isInteger(limit) || limit < 1 || limit > GREP_LIMIT_MAX) {
    throw new SearchError(422, "validation_failed", "Input should be between 1 and 500", "limit");
  }
  if (!Number.isInteger(context) || context < 0 || context > GREP_CONTEXT_MAX) {
    throw new SearchError(422, "validation_failed", "Input should be between 0 and 10", "context");
  }
  const insensitive =
    request.case === "insensitive" || (request.case === "smart" && !/[A-Z]/.test(pattern));
  let compiled: RegExp;
  try {
    compiled = new RegExp(request.regex ? pattern : escapeRegex(pattern), insensitive ? "gi" : "g");
  } catch {
    throw new SearchError(422, "invalid_pattern", "That regular expression can't be used.");
  }
  const globs = request.path_globs ?? [];
  const include = globs.filter((glob) => !glob.startsWith("!")).map(globTest);
  const exclude = globs
    .filter((glob) => glob.startsWith("!"))
    .map((glob) => globTest(glob.slice(1)));
  const hits: GrepHit[] = [];
  let truncated = false;
  for (const { file, lines } of files) {
    if (include.length > 0 && !include.some((test) => test(file.path))) continue;
    if (exclude.some((test) => test(file.path))) continue;
    for (let index = 0; index < lines.length; index += 1) {
      const text = lines[index] ?? "";
      const found = matchesIn(text, compiled).slice(0, MAX_RANGES);
      if (found.length === 0) continue;
      if (hits.length === limit) {
        truncated = true;
        break;
      }
      const ranges = found.map(([start, end]): [number, number] => [
        codePointAt(text, start) + 1,
        codePointAt(text, end) + 1,
      ]);
      const points = Array.from(text);
      const col = ranges[0]?.[0] ?? 1;
      const firstEnd = ranges[0]?.[1] ?? 1;
      const offset =
        points.length > MAX_LINE_CHARS && firstEnd - 1 > MAX_LINE_CHARS
          ? Math.max(0, Math.min(col - 1 - MAX_LINE_CHARS / 5, points.length - MAX_LINE_CHARS))
          : 0;
      hits.push({
        path: file.path,
        line: index + 1,
        col,
        text: points.slice(offset, offset + MAX_LINE_CHARS).join(""),
        offset,
        ranges,
        before: lines.slice(Math.max(0, index - context), index),
        after: lines.slice(index + 1, index + 1 + context),
      });
    }
    if (truncated) break;
  }
  return { commit_sha: commit, hits, truncated, timed_out: false, long_lines: 0 };
}
