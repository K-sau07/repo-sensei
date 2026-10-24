"use client";

/**
 * A preview of some lines of a file, on hover or focus of whatever links to them (a search
 * result): the lines syntax-highlighted as the viewer would show them, the matches marked. The
 * file is read with the viewer's own query, so opening it after is instant.
 */
import { PreviewCard } from "@base-ui/react/preview-card";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Route as RouteIcon } from "lucide-react";
import { useEffect, useState, type CSSProperties, type ReactNode } from "react";

import { KeyHint } from "@/components/primitives/key-hint";
import { useRepo, useRepoSlug } from "@/components/repo/use-repo";
import type { SnippetHighlight } from "@/lib/api/search";
import { languageName } from "@/lib/code/format";
import { describeLines } from "@/lib/code/lines";
import { parentOf } from "@/lib/code/tree";
import { rangesOnLine, type Token } from "@/lib/search/highlight";
import { shortcut } from "@/lib/shortcut-registry";

import { fileOptions, snapshotVersion, treeOptions } from "./use-code";
import { CodeLines, Marked, MarkedTokens } from "../search/snippet";

/** At most this many lines are previewed; the rest are a line count. */
export const PREVIEW_LINES = 30;

export interface CodePreview {
  path: string;
  language: string | null;
  start: number;
  end: number;
  /** Matches to mark, on absolute lines, in UTF-16 columns. */
  highlights: readonly SnippetHighlight[];
  /** Lines already known (a search snippet), shown until the file arrives. */
  fallback: { start: number; lines: readonly string[] };
  /** The commit or branch it's read at (null: the last index). */
  ref: string | null;
}

interface Highlighted {
  key: string;
  lines: Token[][];
  styles: Readonly<Record<string, CSSProperties>>;
}

/** The file, read as the viewer reads it; its directory's listing too, which the code browser
 * needs to show it. */
function usePreviewFile(preview: CodePreview) {
  const { org, repo } = useRepoSlug();
  const repository = useRepo({ refetchOnMount: false });
  const version = snapshotVersion(repository.data);
  const { ref, path } = preview;
  const client = useQueryClient();
  const ready = repository.isSuccess;
  useEffect(() => {
    if (!ready) return;
    client.query(treeOptions({ org, repo, ref, version }, parentOf(path))).catch(() => {
      // The code browser reads it again if it has to.
    });
  }, [client, ready, org, repo, ref, version, path]);
  return useQuery({ ...fileOptions({ org, repo, ref, version }, path), enabled: ready });
}

export function CodeHoverCardContent({ preview }: { preview: CodePreview }) {
  const file = usePreviewFile(preview);
  const content = file.data?.content ?? null;
  const shownEnd = Math.min(preview.end, preview.start + PREVIEW_LINES - 1);
  const key = `${preview.path}:${String(preview.start)}-${String(shownEnd)}`;
  const [highlighted, setHighlighted] = useState<Highlighted | null>(null);

  useEffect(() => {
    if (content === null) return;
    let cancelled = false;
    import("@/lib/code/syntax")
      .then(async ({ tokenizeLines, TOKEN_STYLES }) => {
        const lines = await tokenizeLines(
          preview.path,
          preview.language,
          content,
          preview.start,
          shownEnd,
        );
        if (!cancelled && lines) setHighlighted({ key, lines, styles: TOKEN_STYLES });
      })
      .catch(() => {
        // No grammar (offline, or a chunk failed): the lines stay plain.
      });
    return () => {
      cancelled = true;
    };
  }, [content, key, preview.path, preview.language, preview.start, shownEnd]);

  let start = preview.start;
  let rows: ReactNode[];
  if (content !== null) {
    const text = content.split("\n").slice(preview.start - 1, shownEnd);
    const tokens = highlighted?.key === key ? highlighted : null;
    rows = text.map((line, index) => {
      const ranges = rangesOnLine(preview.highlights, preview.start + index);
      const lineTokens = tokens?.lines[index];
      return lineTokens ? (
        <MarkedTokens key={index} tokens={lineTokens} ranges={ranges} styles={tokens.styles} />
      ) : (
        <Marked key={index} text={line.replace(/\r$/, "")} ranges={ranges} />
      );
    });
  } else {
    start = preview.fallback.start;
    rows = preview.fallback.lines.map((line, index) => (
      <Marked key={index} text={line} ranges={rangesOnLine(preview.highlights, start + index)} />
    ));
  }
  const rest = preview.end - shownEnd;
  const language = languageName(preview.language);

  return (
    <div className="flex flex-col">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-xs text-fg-muted">
        <span className="min-w-0 flex-1 truncate font-mono text-fg">{preview.path}</span>
        <span className="shrink-0 tabular-nums">
          {describeLines({ start: preview.start, end: preview.end })}
        </span>
        {language && <span className="shrink-0">{language}</span>}
      </div>
      <div className="max-h-96 overflow-hidden px-3 py-2" aria-busy={content === null}>
        <CodeLines start={start} lines={rows} />
        {content !== null && rest > 0 && (
          <p className="mt-1 text-xs text-fg-faint">{rest.toLocaleString("en-US")} more lines</p>
        )}
      </div>
      <div className="flex items-center gap-3 border-t border-line px-3 py-2 text-xs text-fg-muted">
        <span className="inline-flex items-center gap-1">
          <KeyHint shortcut={shortcut("list-open").keys} /> Open
        </span>
        <span className="inline-flex items-center gap-1 text-fg-faint" aria-disabled="true">
          <KeyHint shortcut={shortcut("trace").keys} />
          <RouteIcon aria-hidden className="size-3.5" />
          Trace: coming soon
        </span>
      </div>
    </div>
  );
}

/** One preview card for many triggers (`PreviewCard.Trigger` with this handle and a payload). */
export function CodeHoverCard({ handle }: { handle: PreviewCard.Handle<CodePreview> }) {
  return (
    <PreviewCard.Root handle={handle}>
      {({ payload }) => (
        <PreviewCard.Portal>
          <PreviewCard.Positioner side="right" align="start" sideOffset={8} className="z-50">
            <PreviewCard.Popup
              aria-label={payload ? `Preview of ${payload.path}` : "Preview"}
              className="w-[min(36rem,calc(100vw-2rem))] overflow-hidden rounded-[10px] border border-line-strong bg-surface-2 shadow-(--elevation-2) outline-none"
            >
              {payload && <CodeHoverCardContent preview={payload} />}
            </PreviewCard.Popup>
          </PreviewCard.Positioner>
        </PreviewCard.Portal>
      )}
    </PreviewCard.Root>
  );
}
