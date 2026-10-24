/**
 * Line ranges in the code browser's URL: `?L=10` is one line, `?L=10-42` a range (always written
 * low to high). GitHub's `#L10-L42` is accepted too, so pasted links work.
 */
import { createParser } from "nuqs";

export interface LineRange {
  start: number;
  end: number;
}

const RANGE = /^L?(\d{1,9})(?:-L?(\d{1,9}))?$/i;

export function parseLines(value: string): LineRange | null {
  const match = RANGE.exec(value.trim());
  if (!match) return null;
  const first = Number(match[1]);
  const second = match[2] === undefined ? first : Number(match[2]);
  if (first < 1 || second < 1) return null;
  return { start: Math.min(first, second), end: Math.max(first, second) };
}

export function formatLines({ start, end }: LineRange): string {
  return start === end ? String(start) : `${String(start)}-${String(end)}`;
}

export const sameLines = (a: LineRange | null, b: LineRange | null): boolean =>
  a === b || (a !== null && b !== null && a.start === b.start && a.end === b.end);

/** The range within a file of `lineCount` lines; null when it starts past the end. */
export function clampLines(range: LineRange, lineCount: number): LineRange | null {
  if (range.start > lineCount) return null;
  return { start: range.start, end: Math.min(range.end, lineCount) };
}

/** A click on line `line`; with Shift, the range grows from the line picked before. */
export function extendLines(current: LineRange | null, line: number, extend: boolean): LineRange {
  if (!extend || !current) return { start: line, end: line };
  const anchor = line < current.start ? current.end : current.start;
  return { start: Math.min(anchor, line), end: Math.max(anchor, line) };
}

/** "Lines 10–42" or "Line 10", for labels and announcements. */
export function describeLines({ start, end }: LineRange): string {
  return start === end ? `Line ${String(start)}` : `Lines ${String(start)}–${String(end)}`;
}

/** `?L=` for nuqs. */
export const parseAsLines = createParser({
  parse: parseLines,
  serialize: formatLines,
  eq: sameLines,
});
