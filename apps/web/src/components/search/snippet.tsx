import { cn } from "cn";
import type { CSSProperties, ReactNode } from "react";

import type { SnippetHighlight } from "@/lib/api/search";
import {
  markTokens,
  rangesOnLine,
  segments,
  windowLine,
  type TextRange,
  type Token,
} from "@/lib/search/highlight";

/** How a match looks: the warn tint the code viewer lights lines with, stronger. */
export const MARK_CLASS = "rounded-[2px] bg-warn/30 text-fg ring-1 ring-warn/40";

/** Plain text with its marks (as text nodes and `<mark>`, never HTML). */
export function Marked({ text, ranges }: { text: string; ranges: readonly TextRange[] }) {
  return (
    <>
      {segments(text, ranges).map((part, index) =>
        part.mark ? (
          <mark key={index} className={MARK_CLASS}>
            {part.text}
          </mark>
        ) : (
          part.text
        ),
      )}
    </>
  );
}

/** Syntax tokens with their marks. */
export function MarkedTokens({
  tokens,
  ranges,
  styles,
}: {
  tokens: readonly Token[];
  ranges: readonly TextRange[];
  styles: Readonly<Record<string, CSSProperties>>;
}) {
  return (
    <>
      {markTokens(tokens, ranges).map((token, index) => {
        const style = token.tag ? styles[token.tag] : undefined;
        if (token.mark) {
          return (
            <mark key={index} className={MARK_CLASS} style={style}>
              {token.text}
            </mark>
          );
        }
        return style ? (
          <span key={index} style={style}>
            {token.text}
          </span>
        ) : (
          token.text
        );
      })}
    </>
  );
}

export interface CodeLinesProps {
  /** The first line's number. */
  start: number;
  lines: readonly ReactNode[];
  className?: string | undefined;
}

/** Numbered lines of code (the numbers aren't read out: the card names the lines). */
export function CodeLines({ start, lines, className }: CodeLinesProps) {
  return (
    <div
      className={cn(
        "grid grid-cols-[auto_minmax(0,1fr)] font-mono text-[12px] leading-[1.6]",
        className,
      )}
    >
      {lines.map((line, index) => (
        <div key={start + index} className="contents">
          <span aria-hidden className="pr-3 text-right text-fg-faint tabular-nums select-none">
            {start + index}
          </span>
          <code className="block overflow-hidden text-ellipsis whitespace-pre">{line}</code>
        </div>
      ))}
    </div>
  );
}

const CARD_LINE_CHARS = 160;

/** The lines of a snippet worth showing in a card: up to `max`, from just above its first
 * match. */
export function cardWindow(
  snippet: {
    start_line: number;
    lines: readonly string[];
    highlights: readonly SnippetHighlight[];
  },
  max: number,
): { start: number; lines: readonly string[] } {
  const { start_line: start, lines, highlights } = snippet;
  if (lines.length <= max) return { start, lines };
  const first = highlights[0]?.line ?? start;
  const from = Math.max(0, Math.min(first - start - 1, lines.length - max));
  return { start: start + from, lines: lines.slice(from, from + max) };
}

/** A result's snippet, its matches marked; a long line is cut to show its first match. */
export function SnippetView({
  snippet,
  maxLines,
  className,
}: {
  snippet: {
    start_line: number;
    lines: readonly string[];
    highlights: readonly SnippetHighlight[];
  };
  maxLines: number;
  className?: string | undefined;
}) {
  const { start, lines } = cardWindow(snippet, maxLines);
  return (
    <CodeLines
      start={start}
      className={className}
      lines={lines.map((text, index) => {
        const shown = windowLine(
          text,
          rangesOnLine(snippet.highlights, start + index),
          CARD_LINE_CHARS,
        );
        return (
          <>
            {shown.cutStart && <span className="text-fg-faint">…</span>}
            <Marked text={shown.text} ranges={shown.ranges} />
            {shown.cutEnd && <span className="text-fg-faint">…</span>}
          </>
        );
      })}
    />
  );
}
