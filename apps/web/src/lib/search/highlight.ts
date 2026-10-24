/**
 * Highlight ranges on lines of code, for rendering as text and `<mark>` elements (never as HTML).
 * Search ranges are UTF-16 code units, end exclusive, as JavaScript strings index; exact search
 * (grep) reports characters, so those are converted first.
 */
import type { SnippetHighlight } from "@/lib/api/search";

export interface TextRange {
  start: number;
  end: number;
}

export interface Segment {
  text: string;
  mark: boolean;
}

const isHigh = (code: number) => code >= 0xd800 && code <= 0xdbff;
const isLow = (code: number) => code >= 0xdc00 && code <= 0xdfff;

/** An offset moved off the middle of a surrogate pair (`towards` the pair's start or end), so a
 * character is never cut in two. */
function onBoundary(text: string, offset: number, towards: "start" | "end"): number {
  if (offset <= 0 || offset >= text.length) return offset;
  if (isHigh(text.charCodeAt(offset - 1)) && isLow(text.charCodeAt(offset))) {
    return towards === "start" ? offset - 1 : offset + 1;
  }
  return offset;
}

/** The ranges inside `text`, widened to whole characters, in order, with overlapping or touching
 * ones merged; empty and out-of-range ones dropped. */
export function normalizeRanges(text: string, ranges: readonly TextRange[]): TextRange[] {
  const clipped = ranges
    .filter((range) => Number.isFinite(range.start) && Number.isFinite(range.end))
    .map((range) => ({
      start: onBoundary(text, Math.max(0, Math.floor(range.start)), "start"),
      end: onBoundary(text, Math.min(text.length, Math.ceil(range.end)), "end"),
    }))
    .filter((range) => range.end > range.start)
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const merged: TextRange[] = [];
  for (const range of clipped) {
    const last = merged.at(-1);
    if (last && range.start <= last.end) last.end = Math.max(last.end, range.end);
    else merged.push({ ...range });
  }
  return merged;
}

/** `text` cut into plain and marked runs. */
export function segments(text: string, ranges: readonly TextRange[]): Segment[] {
  const out: Segment[] = [];
  let at = 0;
  for (const { start, end } of normalizeRanges(text, ranges)) {
    if (start > at) out.push({ text: text.slice(at, start), mark: false });
    out.push({ text: text.slice(start, end), mark: true });
    at = end;
  }
  if (at < text.length || out.length === 0) out.push({ text: text.slice(at), mark: false });
  return out;
}

/** The ranges a snippet's highlights put on one (absolute) line. */
export function rangesOnLine(highlights: readonly SnippetHighlight[], line: number): TextRange[] {
  return highlights.filter((mark) => mark.line === line).map(({ start, end }) => ({ start, end }));
}

export interface LineWindow {
  text: string;
  ranges: TextRange[];
  /** Text was left out before or after. */
  cutStart: boolean;
  cutEnd: boolean;
}

/** A long line cut to about `max` units, starting a little before its first mark so the mark is
 * in view (a minified file's only line can be thousands of characters long). */
export function windowLine(text: string, ranges: readonly TextRange[], max: number): LineWindow {
  const marks = normalizeRanges(text, ranges);
  if (text.length <= max) return { text, ranges: marks, cutStart: false, cutEnd: false };
  const first = marks[0]?.start ?? 0;
  const lead = Math.floor(max / 5);
  const from = onBoundary(text, Math.max(0, Math.min(first - lead, text.length - max)), "start");
  const to = onBoundary(text, Math.min(text.length, from + max), "end");
  return {
    text: text.slice(from, to),
    ranges: normalizeRanges(
      text.slice(from, to),
      marks.map((range) => ({ start: range.start - from, end: range.end - from })),
    ),
    cutStart: from > 0,
    cutEnd: to < text.length,
  };
}

/** UTF-16 offsets for character offsets (code points) into `text`. */
function utf16Offsets(text: string): number[] {
  const offsets = [0];
  let at = 0;
  for (const char of text) {
    at += char.length;
    offsets.push(at);
  }
  return offsets;
}

/**
 * Exact search's ranges (1-based character columns of the whole line, end exclusive) as UTF-16
 * ranges in `text`, the part of the line that starts `offset` characters in.
 */
export function fromCharacterColumns(
  text: string,
  offset: number,
  ranges: readonly (readonly [number, number])[],
): TextRange[] {
  const offsets = utf16Offsets(text);
  const last = offsets.length - 1;
  const at = (column: number) => offsets[Math.max(0, Math.min(last, column - 1 - offset))] ?? 0;
  return normalizeRanges(
    text,
    ranges.map(([start, end]) => ({ start: at(start), end: at(end) })),
  );
}

export interface Token {
  text: string;
  /** The syntax class (see lib/code/syntax.ts); null for plain text. */
  tag: string | null;
}

export type MarkedToken = Token & { mark: boolean };

/** Syntax tokens of one line, split where the marks start and end. */
export function markTokens(tokens: readonly Token[], ranges: readonly TextRange[]): MarkedToken[] {
  const line = tokens.map((token) => token.text).join("");
  const marks = normalizeRanges(line, ranges);
  const out: MarkedToken[] = [];
  let at = 0;
  for (const token of tokens) {
    const from = at;
    const to = at + token.text.length;
    at = to;
    let cursor = from;
    for (const mark of marks) {
      if (mark.end <= cursor || mark.start >= to) continue;
      const start = Math.max(mark.start, cursor);
      const end = Math.min(mark.end, to);
      if (start > cursor) out.push({ ...token, text: line.slice(cursor, start), mark: false });
      out.push({ ...token, text: line.slice(start, end), mark: true });
      cursor = end;
    }
    if (cursor < to) out.push({ ...token, text: line.slice(cursor, to), mark: false });
  }
  return out;
}
