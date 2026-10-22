/**
 * The palette's fuzzy matcher: the query's characters in order (a subsequence), scored higher at
 * word starts (after a space, `/`, `_`, `-`, `.`…), camelCase humps and digits, and for runs of
 * consecutive characters. Scores are normalised to 0–1 so different sources can be merged.
 *
 * The same module runs on the main thread (small sets, so results come in the frame the palette
 * opens) and in the fuzzy worker (larger sets). It's pure: identical input, identical output.
 */
import type { Matchable, MatchRange } from "./types";

const SCORE_MATCH = 16;
const BONUS_FIRST = 10; // the text's first character
const BONUS_BOUNDARY = 9; // after a separator
const BONUS_CAMEL = 8; // fooBar, v2
const BONUS_CONSECUTIVE = 5; // the least a character right after the last match gets
const GAP_START = -3;
const GAP_EXTEND = -1;
const LEADING_PER_CHAR = -0.25; // matches nearer the start win ties
const LEADING_MAX = -3;
/** Each extra separate piece of the text a match needs costs this share of its score. */
const FRAGMENT_PENALTY = 0.1;
/** Longer texts with the same match rank a little lower (at most this much). */
const LENGTH_PENALTY_PER_CHAR = 0.002;
const LENGTH_PENALTY_MAX = 0.1;

const MAX_QUERY = 64;
const MAX_TEXT = 512;

/** Field weights: a match in the title counts most, a hidden keyword next, the subtitle least. */
const WEIGHT_TITLE = 1;
const WEIGHT_KEYWORD = 0.9;
const WEIGHT_SUBTITLE = 0.8;
/** A match through a synonym ("remove" finding "Delete") ranks below a literal one. */
const SYNONYM_WEIGHT = 0.92;

export interface TextMatch {
  /** 0–1: 1 is the text itself. */
  score: number;
  ranges: MatchRange[];
}

export interface ItemMatch {
  id: string;
  score: number;
  title: MatchRange[];
  subtitle: MatchRange[];
  /** It matched only through a synonym (never the literal query). */
  synonym: boolean;
}

// ----------------------------------------------------------------- characters

const isUpper = (code: number) => code >= 65 && code <= 90;
const isLower = (code: number) => code >= 97 && code <= 122;
const isDigit = (code: number) => code >= 48 && code <= 57;

function isWordChar(char: string): boolean {
  const code = char.charCodeAt(0);
  if (isUpper(code) || isLower(code) || isDigit(code)) return true;
  // Letters outside ASCII have case (or are at least not punctuation).
  return code > 127 && char.toLowerCase() !== char.toUpperCase();
}

/** A character and the combining marks after it, kept together: a highlight never cuts an
 * accent off its letter, however the text is normalised. */
const CLUSTER = /\P{M}\p{M}*|\p{M}+/gu;
const MARK = /\p{M}/u;
const MARKS = /\p{M}/gu;

/** What a cluster is compared as: NFC, lowercased. Lowercasing can add marks ("İ" → "i" plus a
 * dot above); those are dropped, so "i" finds "İstanbul". */
function fold(cluster: string): string {
  const composed = cluster.normalize("NFC");
  const lowered = composed.toLowerCase().normalize("NFC");
  return MARK.test(composed) ? lowered : lowered.replace(MARKS, "");
}

/** A query as it's compared. */
export function foldText(text: string): string {
  let out = "";
  for (const [cluster] of text.matchAll(CLUSTER)) out += fold(cluster);
  return out;
}

/** A text ready to match against: folded characters, each with the range of the original text
 * it came from, and the bonus a match there earns. */
interface Prepared {
  chars: string;
  start: number[];
  end: number[];
  bonus: number[];
}

function bonusAt(prev: string | undefined, char: string): number {
  if (prev === undefined) return BONUS_FIRST;
  const code = char.charCodeAt(0);
  const prevCode = prev.charCodeAt(0);
  if (!isWordChar(prev) && isWordChar(char)) return BONUS_BOUNDARY;
  if (isLower(prevCode) && isUpper(code)) return BONUS_CAMEL;
  if (!isDigit(prevCode) && isWordChar(prev) && isDigit(code)) return BONUS_CAMEL;
  return 0;
}

function prepare(text: string): Prepared {
  const prepared: Prepared = { chars: "", start: [], end: [], bonus: [] };
  let prev: string | undefined;
  for (const match of text.matchAll(CLUSTER)) {
    const cluster = match[0];
    const base = String.fromCodePoint(cluster.codePointAt(0) ?? 32);
    const bonus = bonusAt(prev, base);
    const folded = fold(cluster);
    for (let k = 0; k < folded.length; k += 1) {
      prepared.chars += folded.charAt(k);
      prepared.start.push(match.index);
      prepared.end.push(match.index + cluster.length);
      prepared.bonus.push(k === 0 ? bonus : 0);
    }
    prev = base;
  }
  return prepared;
}

/** Union of ranges, sorted and merged. */
export function mergeRanges(ranges: readonly MatchRange[]): MatchRange[] {
  const sorted = [...ranges].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const out: [number, number][] = [];
  for (const [start, end] of sorted) {
    const last = out.at(-1);
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else out.push([start, end]);
  }
  return out;
}

// ----------------------------------------------------------------- one word against one text

/**
 * The best way to match `query` (one word, no spaces) as a subsequence of `text`, or null.
 * Dynamic programming over (query position, text position), O(query × text).
 */
export function fuzzyMatch(query: string, text: string): TextMatch | null {
  const q = foldText(query.slice(0, MAX_QUERY));
  const prepared = prepare(text.slice(0, MAX_TEXT));
  const t = prepared.chars;
  const m = q.length;
  const n = t.length;
  if (m === 0) return { score: 0, ranges: [] };
  if (m > n) return null;

  // Cheap rejection, and the window a match can use: from the earliest start to the latest end.
  let first = -1;
  let found = 0;
  for (let j = 0; j < n && found < m; j += 1) {
    if (t[j] === q[found]) {
      if (found === 0) first = j;
      found += 1;
    }
  }
  if (found < m) return null;
  let last = -1;
  found = m - 1;
  for (let j = n - 1; j >= 0 && found >= 0; j -= 1) {
    if (t[j] === q[found]) {
      if (found === m - 1) last = j;
      found -= 1;
    }
  }

  const bonus = prepared.bonus;
  const size = m * n;
  const score = new Float64Array(size).fill(-Infinity);
  const from = new Int32Array(size).fill(-1);
  // The bonus of the run a match continues: a run that starts at a word start keeps its bonus.
  const runBonus = new Float64Array(size);

  for (let j = first; j <= last; j += 1) {
    if (t[j] !== q[0]) continue;
    const b = bonus[j] ?? 0;
    score[j] = SCORE_MATCH + b + Math.max(LEADING_MAX, LEADING_PER_CHAR * j);
    runBonus[j] = b;
  }
  for (let i = 1; i < m; i += 1) {
    const row = i * n;
    const prev = (i - 1) * n;
    let gapBest = -Infinity;
    let gapFrom = -1;
    for (let j = first + i; j <= last; j += 1) {
      // Best previous match at least two characters back, paying for the gap since.
      if (j >= 2) {
        gapBest += GAP_EXTEND;
        const candidate = (score[prev + j - 2] ?? -Infinity) + GAP_START;
        if (candidate > gapBest) {
          gapBest = candidate;
          gapFrom = j - 2;
        }
      }
      if (t[j] !== q[i]) continue;
      const b = bonus[j] ?? 0;
      let best = -Infinity;
      let bestFrom = -1;
      let bestRun = b;
      const diagonal = score[prev + j - 1] ?? -Infinity;
      if (diagonal > -Infinity) {
        const run = Math.max(runBonus[prev + j - 1] ?? 0, b, BONUS_CONSECUTIVE);
        best = diagonal + SCORE_MATCH + run;
        bestFrom = j - 1;
        bestRun = run;
      }
      if (gapBest > -Infinity) {
        const gapped = gapBest + SCORE_MATCH + b;
        if (gapped > best) {
          best = gapped;
          bestFrom = gapFrom;
          bestRun = b;
        }
      }
      score[row + j] = best;
      from[row + j] = bestFrom;
      runBonus[row + j] = bestRun;
    }
  }

  const lastRow = (m - 1) * n;
  let end = -1;
  let raw = -Infinity;
  for (let j = first; j <= last; j += 1) {
    const value = score[lastRow + j] ?? -Infinity;
    if (value > raw) {
      raw = value;
      end = j;
    }
  }
  if (end < 0) return null;

  const positions = new Array<number>(m);
  let at = end;
  for (let i = m - 1; i >= 0; i -= 1) {
    positions[i] = at;
    at = from[i * n + at] ?? -1;
  }
  // Back to the original text, whole clusters at a time.
  const ranges = mergeRanges(
    positions.map((p): MatchRange => [prepared.start[p] ?? 0, prepared.end[p] ?? 0]),
  );

  if (t === q) return { score: 1, ranges };
  // The best any text could do: the query as the start of the text.
  const ideal = (SCORE_MATCH + BONUS_FIRST) * m;
  let normalised = Math.min(raw / ideal, 0.99);
  normalised *= Math.max(0.3, 1 - FRAGMENT_PENALTY * (ranges.length - 1));
  normalised -= Math.min(LENGTH_PENALTY_MAX, LENGTH_PENALTY_PER_CHAR * (n - m));
  return { score: Math.max(0.01, normalised), ranges };
}

// ----------------------------------------------------------------- synonyms

/** Words people use for the same thing. A query using one also finds items using another. */
export const SYNONYMS: readonly (readonly string[])[] = [
  ["delete", "remove"],
  ["settings", "preferences"],
  ["reindex", "refresh", "rebuild"],
  ["sign out", "log out", "logout"],
  ["theme", "dark", "light"],
];

/** The shortest unfinished word that already counts as a synonym ("refre" → "refresh"). Any
 * shorter and "log" would read as "sign out", "the" as "dark", "rem" as "delete". */
export const SYNONYM_PREFIX_MIN = 5;

function normaliseQuery(query: string): string {
  return foldText(query).trim().replace(/\s+/g, " ");
}

/** The query, plus each way of reading it through a synonym. The literal query comes first. */
export function queryVariants(query: string): { text: string; synonym: boolean }[] {
  const q = normaliseQuery(query);
  if (!q) return [];
  const out = new Map<string, boolean>([[q, false]]);
  const padded = ` ${q} `;
  for (const group of SYNONYMS) {
    for (const phrase of group) {
      const alternatives = group.filter((other) => other !== phrase);
      if (padded.includes(` ${phrase} `)) {
        for (const other of alternatives) {
          const text = padded.split(` ${phrase} `).join(` ${other} `).trim();
          if (!out.has(text)) out.set(text, true);
        }
        continue;
      }
      // The word being typed: its start may already be the phrase ("refre", "sign o").
      for (let cut = q.length - SYNONYM_PREFIX_MIN; cut >= 0; cut -= 1) {
        if (cut > 0 && q[cut - 1] !== " ") continue;
        const tail = q.slice(cut);
        if (tail.length < SYNONYM_PREFIX_MIN || !phrase.startsWith(tail) || tail === phrase) {
          continue;
        }
        for (const other of alternatives) {
          const text = `${q.slice(0, cut)}${other}`;
          if (!out.has(text)) out.set(text, true);
        }
        break;
      }
    }
  }
  return [...out].map(([text, synonym]) => ({ text, synonym }));
}

// ----------------------------------------------------------------- items

function matchWords(words: readonly string[], item: Matchable): ItemMatch | null {
  let total = 0;
  const title: MatchRange[] = [];
  const subtitle: MatchRange[] = [];
  for (const word of words) {
    let best = 0;
    let bestTitle: MatchRange[] = [];
    let bestSubtitle: MatchRange[] = [];
    const inTitle = fuzzyMatch(word, item.title);
    if (inTitle && inTitle.score * WEIGHT_TITLE > best) {
      best = inTitle.score * WEIGHT_TITLE;
      bestTitle = inTitle.ranges;
      bestSubtitle = [];
    }
    for (const keyword of item.keywords ?? []) {
      const inKeyword = fuzzyMatch(word, keyword);
      if (inKeyword && inKeyword.score * WEIGHT_KEYWORD > best) {
        best = inKeyword.score * WEIGHT_KEYWORD;
        bestTitle = [];
        bestSubtitle = [];
      }
    }
    if (item.subtitle) {
      const inSubtitle = fuzzyMatch(word, item.subtitle);
      if (inSubtitle && inSubtitle.score * WEIGHT_SUBTITLE > best) {
        best = inSubtitle.score * WEIGHT_SUBTITLE;
        bestTitle = [];
        bestSubtitle = inSubtitle.ranges;
      }
    }
    if (best === 0) return null; // every word has to match somewhere
    total += best;
    title.push(...bestTitle);
    subtitle.push(...bestSubtitle);
  }
  return {
    id: item.id,
    score: total / words.length,
    title: mergeRanges(title),
    subtitle: mergeRanges(subtitle),
    synonym: false,
  };
}

/** How well `query` (any number of words, synonyms included) matches the item, or null. */
export function matchItem(query: string, item: Matchable): ItemMatch | null {
  const variants = queryVariants(query);
  if (variants.length === 0) {
    return { id: item.id, score: 0, title: [], subtitle: [], synonym: false };
  }
  let best: ItemMatch | null = null;
  for (const variant of variants) {
    const found = matchWords(variant.text.split(" "), item);
    if (!found) continue;
    const score = found.score * (variant.synonym ? SYNONYM_WEIGHT : 1);
    if (!best || score > best.score) best = { ...found, score, synonym: variant.synonym };
  }
  return best;
}

/** Every item the query matches, in the order given (ranking sorts them). An empty query
 * matches everything with score 0. */
export function matchItems(query: string, items: readonly Matchable[]): ItemMatch[] {
  const out: ItemMatch[] = [];
  for (const item of items) {
    const found = matchItem(query, item);
    if (found) out.push(found);
  }
  return out;
}
