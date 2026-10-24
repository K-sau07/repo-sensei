"use client";

import { PreviewCard } from "@base-ui/react/preview-card";
import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, type MouseEvent } from "react";

import type { CodePreview } from "@/components/code/code-hover-card";
import type { SearchHit } from "@/lib/api/search";
import { formatLines } from "@/lib/code/lines";
import { canTransition, codeViewKey, openCode } from "@/lib/code/transition";
import { codePath } from "@/lib/routes";

import { kindLabel, REASONS } from "./meta";
import { SnippetView } from "./snippet";
import { CARD_ATTR } from "./use-result-keys";

/** Snippet lines a card shows (the preview shows the rest). */
export const CARD_LINES = 4;
const HOVER_DELAY_MS = 400;
const HOVER_CLOSE_MS = 150;

/** A result card; its focus ring is the link's (keyboard focus only). */
export const CARD_CLASS =
  "relative rounded-[10px] border border-line bg-surface-1 px-3 py-2.5 transition-colors duration-(--dur-fast) hover:border-line-strong hover:bg-surface-2 has-[[data-card]:focus-visible]:border-line-strong has-[[data-card]:focus-visible]:ring-2 has-[[data-card]:focus-visible]:ring-ring/70";
/** The card's link: its name and place are its text, and the whole card is its target (the
 * snippet and chips sit under it). */
export const CARD_LINK_CLASS =
  "block outline-none after:absolute after:inset-0 after:rounded-[10px] after:content-['']";

export interface CardTarget {
  org: string;
  repo: string;
  ref: string | null;
}

export function hitPreview(hit: SearchHit, ref: string | null): CodePreview {
  return {
    path: hit.path,
    language: hit.language,
    start: hit.start_line,
    end: hit.end_line,
    highlights: hit.snippet.highlights,
    fallback: { start: hit.snippet.start_line, lines: hit.snippet.lines },
    ref,
  };
}

/** A card's click: the code opens with the card morphing into the viewer (a plain navigation
 * when that can't run; a click with a modifier is the browser's, a new tab say). */
export function useOpenCode() {
  const router = useRouter();
  return (event: MouseEvent<HTMLAnchorElement>, href: Route, key: string) => {
    const modified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
    if (event.defaultPrevented || event.button !== 0 || modified || !canTransition()) return;
    event.preventDefault();
    openCode(
      () => {
        router.push(href);
      },
      event.currentTarget.parentElement ?? event.currentTarget,
      key,
    );
  };
}

/** Why it matched, as text chips (strongest first). */
export function WhyChips({ why, id }: { why: SearchHit["why"]; id?: string }) {
  return (
    <ul id={id} aria-label="Why it matched" className="flex flex-wrap gap-1">
      {why.map((reason) => {
        const { label, explain, icon: Icon } = REASONS[reason];
        return (
          <li
            key={reason}
            title={explain}
            className="inline-flex h-5 items-center gap-1 rounded-sm border border-line px-1.5 text-[11px] text-fg-muted"
          >
            <Icon aria-hidden className="size-3" />
            {label}
          </li>
        );
      })}
    </ul>
  );
}

export interface ResultCardProps {
  hit: SearchHit;
  target: CardTarget;
  preview: PreviewCard.Handle<CodePreview>;
  /** In the tab order (the lane's roving stop). */
  tabbable: boolean;
  onFocus: () => void;
}

/** A result: name, kind, path and lines, the snippet with its matches, and why it matched. A
 * link to the code at its lines; hovering or focusing it previews them. */
export function ResultCard({ hit, target, preview, tabbable, onFocus }: ResultCardProps) {
  const whyId = useId();
  const open = useOpenCode();
  const lines = { start: hit.start_line, end: hit.end_line };
  const href = codePath(target.org, target.repo, hit.path, {
    ref: target.ref,
    lines: formatLines(lines),
  });
  const kind = kindLabel(hit.kind);
  const lineText =
    hit.end_line === hit.start_line
      ? String(hit.start_line)
      : `${String(hit.start_line)}–${String(hit.end_line)}`;
  return (
    <li>
      <div className={CARD_CLASS}>
        <PreviewCard.Trigger
          handle={preview}
          payload={hitPreview(hit, target.ref)}
          delay={HOVER_DELAY_MS}
          closeDelay={HOVER_CLOSE_MS}
          render={
            <Link
              href={href}
              {...{ [CARD_ATTR]: "" }}
              aria-describedby={whyId}
              tabIndex={tabbable ? 0 : -1}
              onFocus={onFocus}
              onClick={(event) => {
                open(event, href, codeViewKey(hit.path, lines) ?? "");
              }}
              className={CARD_LINK_CLASS}
            />
          }
        >
          <span className="flex items-baseline gap-2">
            <span className="min-w-0 truncate font-mono text-[13px] font-semibold text-fg">
              {hit.name}
            </span>
            <span className="shrink-0 text-xs text-fg-muted">{kind}</span>
          </span>
          <span className="mt-0.5 flex min-w-0 text-xs text-fg-muted">
            {/* The start of a long path is cut, so the file name stays in view. */}
            <span className="min-w-0 truncate [direction:rtl]">
              <bdi>
                {hit.path}:{lineText}
              </bdi>
            </span>
          </span>
        </PreviewCard.Trigger>
        <SnippetView
          snippet={hit.snippet}
          maxLines={CARD_LINES}
          className="mt-2 rounded-md bg-bg px-2 py-1.5 text-fg-muted"
        />
        <div className="mt-2">
          <WhyChips why={hit.why} id={whyId} />
        </div>
      </div>
    </li>
  );
}
