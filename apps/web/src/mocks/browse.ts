/**
 * The mock snapshot the code browser reads: the real tree of the fastapi template at b1125d8
 * (fixtures/browse-tree.json, the files discovery kept in the same run index-run.json recorded),
 * so counts agree with the mock repositories: 245 files, 18,567 lines, 54 directories. Content
 * comes from fixtures/browse-content.json for a selection of files (loaded on first use); the
 * rest serve a placeholder that says so.
 *
 * The real tree has no file over 2 MB and nothing long enough for the paint budget, so
 *   localStorage.setItem("sensei.mock.browse", "extras")
 * adds `mock-extras/`: a 10,000-line file, a 3 MB file (413) and a directory of 100,000 files.
 */
import type { FileSearch, TreeEntry } from "@/lib/api/types";

import tree from "./fixtures/browse-tree.json";

export const BROWSE_COMMIT = tree.commit_sha;
export const BROWSE_EXTRAS_KEY = "sensei.mock.browse";
export const MAX_SERVED_BYTES = 2 * 1024 * 1024;

export interface MockFile {
  path: string;
  size: number;
  language: string | null;
  loc: number | null;
  role: string;
  flags: string[];
  /** Git's object id. */
  sha: string;
  /** SHA-256 of the content, hex (the file ETag). */
  content_hash: string;
}

export interface MockDir {
  path: string;
  /** The Merkle hash, hex (the tree ETag); synthetic directories have none. */
  hash: string | null;
}

const FILES = new Map<string, MockFile>(tree.files.map((file) => [file.path, file]));
const DIRS = new Map<string, MockDir>(tree.dirs.map((dir) => [dir.path, dir]));

export function browseExtras(): boolean {
  return (
    typeof localStorage !== "undefined" && localStorage.getItem(BROWSE_EXTRAS_KEY) === "extras"
  );
}

// ----------------------------------------------------------------- synthetic extras

export const EXTRAS_DIR = "mock-extras";
export const LONG_FILE = `${EXTRAS_DIR}/ten_thousand_lines.py`;
export const LARGE_FILE = `${EXTRAS_DIR}/server.log`;
export const MANY_DIR = `${EXTRAS_DIR}/many-files`;
export const MANY_COUNT = 100_000;
const LONG_LINES = 10_000;
const LARGE_BYTES = 3 * 1024 * 1024;

let longFile: string | null = null;

/** 10,000 lines of plausible Python: 2,500 four-line functions. */
export function longFileContent(): string {
  longFile ??= buildLongFile();
  return longFile;
}

function buildLongFile(): string {
  const lines: string[] = [];
  for (let n = 1; lines.length < LONG_LINES; n += 1) {
    const id = String(n).padStart(4, "0");
    lines.push(
      `def handler_${id}(request: dict[str, object]) -> dict[str, object]:`,
      `    """Answer request ${id} with its id and a greeting."""`,
      `    return {"id": ${String(n)}, "message": f"hello {request.get('name', 'world')}"}`,
      "",
    );
  }
  return `${lines.join("\n")}\n`;
}

const manyName = (index: number) => `module_${String(index).padStart(6, "0")}.ts`;

function extraFiles(): MockFile[] {
  // Synthetic files have made-up hashes (only their shape matters).
  const placeholderHash = (seed: string) => seed.repeat(32);
  return [
    {
      path: LONG_FILE,
      size: longFileContent().length,
      language: "python",
      loc: LONG_LINES,
      role: "service",
      flags: [],
      sha: placeholderHash("a1").slice(0, 40),
      content_hash: placeholderHash("a1"),
    },
    {
      path: LARGE_FILE,
      size: LARGE_BYTES,
      language: null,
      loc: null,
      role: "other",
      flags: ["too_large"],
      sha: placeholderHash("b2").slice(0, 40),
      content_hash: placeholderHash("b2"),
    },
  ];
}

function manyFile(index: number): MockFile {
  const hex = index.toString(16).padStart(8, "0");
  return {
    path: `${MANY_DIR}/${manyName(index)}`,
    size: 120,
    language: "typescript",
    loc: 3,
    role: "ui",
    flags: [],
    sha: hex.repeat(5),
    content_hash: hex.repeat(8),
  };
}

// ----------------------------------------------------------------- lookups

/** The file recorded at `path`, if discovery kept one there. */
export function fileAt(path: string): MockFile | undefined {
  const found = FILES.get(path);
  if (found || !browseExtras()) return found;
  const extra = extraFiles().find((file) => file.path === path);
  if (extra) return extra;
  const many = new RegExp(`^${MANY_DIR}/module_(\\d{6})\\.ts$`).exec(path);
  const index = many ? Number(many[1]) : 0;
  return index >= 1 && index <= MANY_COUNT ? manyFile(index) : undefined;
}

export function dirAt(path: string): MockDir | undefined {
  if (browseExtras()) {
    // The root changes when the extras are in, so its recorded hash no longer applies.
    if (path === "") return { path, hash: null };
    if (path === EXTRAS_DIR || path === MANY_DIR) return { path, hash: null };
  }
  return DIRS.get(path);
}

const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);
const nameOf = (path: string) => path.slice(path.lastIndexOf("/") + 1);

/** The real API's order: directories first, then by name, case-insensitively. */
function byKindThenName(a: TreeEntry, b: TreeEntry): number {
  if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
  const left = a.name.toLowerCase();
  const right = b.name.toLowerCase();
  if (left !== right) return left < right ? -1 : 1;
  return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
}

function fileEntry(file: MockFile): TreeEntry {
  return {
    name: nameOf(file.path),
    path: file.path,
    type: "file",
    language: file.language,
    role: file.role,
    size: file.size,
    loc: file.loc,
    flags: file.flags,
    child_count: null,
  };
}

const dirEntry = (path: string, childCount: number): TreeEntry => ({
  name: nameOf(path),
  path,
  type: "dir",
  language: null,
  role: null,
  size: null,
  loc: null,
  flags: null,
  child_count: childCount,
});

/** The direct children of a directory, as `GET /tree` lists them. */
export function listing(dir: string): TreeEntry[] {
  if (browseExtras() && dir === MANY_DIR) {
    return Array.from({ length: MANY_COUNT }, (_, i) => fileEntry(manyFile(i + 1)));
  }
  if (browseExtras() && dir === EXTRAS_DIR) {
    return [dirEntry(MANY_DIR, MANY_COUNT), ...extraFiles().map(fileEntry)].sort(byKindThenName);
  }
  const prefix = dir ? `${dir}/` : "";
  const files: TreeEntry[] = [];
  const dirs = new Map<string, number>();
  for (const file of FILES.values()) {
    if (!file.path.startsWith(prefix)) continue;
    const rest = file.path.slice(prefix.length);
    const slash = rest.indexOf("/");
    if (slash === -1) files.push(fileEntry(file));
    else dirs.set(rest.slice(0, slash), 0);
  }
  const entries = [
    ...[...dirs.keys()].map((name) => dirEntry(join(dir, name), listing(join(dir, name)).length)),
    ...files,
  ];
  if (browseExtras() && dir === "") entries.push(dirEntry(EXTRAS_DIR, 3));
  return entries.sort(byKindThenName);
}

// ----------------------------------------------------------------- content

interface ContentFixture {
  files: Record<string, string>;
}

let content: Promise<ContentFixture> | null = null;

/** The real content the fixture has, by path (the files the mock search reads). */
export async function contentFiles(): Promise<Readonly<Record<string, string>>> {
  content ??= import("./fixtures/browse-content.json").then((module) => module.default);
  return (await content).files;
}

/** The file's text: the real content when the fixture has it, otherwise a placeholder. */
export async function textOf(file: MockFile): Promise<string> {
  if (file.path === LONG_FILE) return longFileContent();
  if (file.path.startsWith(`${MANY_DIR}/`)) {
    return `export const name = "${nameOf(file.path)}";\nexport const index = 1;\nexport default name;\n`;
  }
  const known = (await contentFiles())[file.path];
  if (known !== undefined) return known;
  return [
    "The mock API has this file's metadata but not its content: only a selection of the",
    `fastapi template's files is in the mock fixture. (${String(file.loc ?? 0)} lines in the repository.)`,
    "",
  ].join("\n");
}

// ----------------------------------------------------------------- search

/** pg_trgm's trigrams of each word (alphanumeric runs, lowercased, padded "  w" … "d "), in order. */
function trigrams(text: string): string[] {
  const out: string[] = [];
  for (const word of text.toLowerCase().split(/[^\p{L}\p{N}]+/u)) {
    if (!word) continue;
    const padded = `  ${word} `;
    for (let i = 0; i + 3 <= padded.length; i += 1) out.push(padded.slice(i, i + 3));
  }
  return out;
}

/** pg_trgm's `similarity(a, b)`: shared trigrams over all of both, |shared| / |a ∪ b|. */
export function similarity(a: string, b: string): number {
  const left = new Set(trigrams(a));
  const right = new Set(trigrams(b));
  let shared = 0;
  for (const trigram of left) if (right.has(trigram)) shared += 1;
  const all = left.size + right.size - shared;
  return all === 0 ? 0 : shared / all;
}

/**
 * pg_trgm's `word_similarity(query, text)`: the best similarity between the query's trigrams
 * and any continuous run of the text's, |shared| / (|query| + |run| − |shared|).
 */
export function wordSimilarity(query: string, text: string): number {
  const wanted = new Set(trigrams(query));
  if (wanted.size === 0) return 0;
  const found = trigrams(text);
  // Cheap bound first: a run can't share more than the whole text does.
  const shared = new Set(found.filter((trigram) => wanted.has(trigram)));
  if (shared.size === 0) return 0;
  let best = 0;
  for (let start = 0; start < found.length; start += 1) {
    const first = found[start];
    if (first === undefined || !wanted.has(first)) continue; // a best run starts on a match
    const run = new Set<string>();
    let common = 0;
    for (let end = start; end < found.length; end += 1) {
      const trigram = found[end];
      if (trigram === undefined || run.has(trigram)) continue;
      run.add(trigram);
      if (wanted.has(trigram)) {
        common += 1;
        best = Math.max(best, common / (wanted.size + run.size - common));
      }
    }
  }
  return best;
}

const inOrder = (needle: string, haystack: string) => {
  let at = 0;
  for (const char of haystack) if (char === needle[at]) at += 1;
  return at >= needle.length;
};

const WORD_SIMILARITY_THRESHOLD = 0.6; // pg_trgm's default for %>

/** `GET /files/search`, ranked the way the API ranks: word similarity on the name (twice) and
 * on the path, plus boosts for a name prefix and a substring of the path. */
export function searchFiles(query: string, limit: number): FileSearch["results"] {
  const lowered = query.toLowerCase();
  const candidates = [...FILES.values(), ...(browseExtras() ? extraFiles() : [])];
  const hits = [];
  for (const file of candidates) {
    const path = file.path.toLowerCase();
    const pathSimilarity = wordSimilarity(lowered, path);
    if (!inOrder(lowered, path) && pathSimilarity < WORD_SIMILARITY_THRESHOLD) continue;
    const name = nameOf(path);
    const score =
      wordSimilarity(lowered, name) * 2 +
      pathSimilarity +
      (name.startsWith(lowered) ? 1 : 0) +
      (path.includes(lowered) ? 0.5 : 0);
    hits.push({ file, score });
  }
  hits.sort(
    (a, b) =>
      b.score - a.score ||
      a.file.path.length - b.file.path.length ||
      (a.file.path < b.file.path ? -1 : a.file.path > b.file.path ? 1 : 0),
  );
  return hits.slice(0, limit).map(({ file, score }) => ({
    path: file.path,
    name: nameOf(file.path),
    language: file.language,
    role: file.role,
    size: file.size,
    score: Math.round(score * 10_000) / 10_000,
  }));
}
