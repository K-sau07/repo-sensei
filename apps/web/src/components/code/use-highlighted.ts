"use client";

/**
 * Lines of a file as syntax tokens, for a preview (a citation's peek): the viewer's grammars
 * and colors (lib/code/syntax.ts, loaded on first use). Null until they're ready, and when the
 * file has no grammar: the lines show plain meanwhile.
 */
import { useEffect, useState, type CSSProperties } from "react";

import type { Token } from "@/lib/search/highlight";

export interface HighlightedLines {
  lines: Token[][];
  styles: Readonly<Record<string, CSSProperties>>;
}

export function useHighlighted(
  path: string,
  language: string | null,
  content: string | null,
  from: number,
  to: number,
): HighlightedLines | null {
  const key = `${path}:${String(from)}-${String(to)}`;
  const [found, setFound] = useState<(HighlightedLines & { key: string }) | null>(null);
  useEffect(() => {
    if (content === null) return;
    let cancelled = false;
    import("@/lib/code/syntax")
      .then(async ({ tokenizeLines, TOKEN_STYLES }) => {
        const lines = await tokenizeLines(path, language, content, from, to);
        if (!cancelled && lines) setFound({ key, lines, styles: TOKEN_STYLES });
      })
      .catch(() => {
        // No grammar (offline, or a chunk failed): the lines stay plain.
      });
    return () => {
      cancelled = true;
    };
  }, [content, key, path, language, from, to]);
  return found?.key === key ? found : null;
}
