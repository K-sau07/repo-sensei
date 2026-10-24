"use client";

import { PreviewCard } from "@base-ui/react/preview-card";
import { cn } from "cn";
import Link from "next/link";
import { useId, useState } from "react";

import { CodeHoverCard, type CodePreview } from "@/components/code/code-hover-card";
import type { GrepHit, GrepResult } from "@/lib/api/types";
import { codeViewKey } from "@/lib/code/transition";
import { codePath } from "@/lib/routes";
import { fromCharacterColumns, windowLine } from "@/lib/search/highlight";

import { plural } from "./meta";
import { CARD_CLASS, CARD_LINK_CLASS, useOpenCode, type CardTarget } from "./result-card";
import { CodeLines, Marked } from "./snippet";

const LINE_CHARS = 200;

function preview(hit: GrepHit, ref: string | null): CodePreview {
  const start = hit.line - hit.before.length;
  // Marks in the preview need the whole line; a long one's text is only part of it.
  const marks = hit.offset === 0 ? fromCharacterColumns(hit.text, 0, hit.ranges) : [];
  return {
    path: hit.path,
    language: null,
    start,
    end: hit.line + hit.after.length,
    highlights: marks.map((range) => ({ line: hit.line, ...range })),
    fallback: { start, lines: [...hit.before, hit.text, ...hit.after] },
    ref,
  };
}

function ExactCard({
  hit,
  target,
  handle,
  tabbable,
  onFocus,
}: {
  hit: GrepHit;
  target: CardTarget;
  handle: PreviewCard.Handle<CodePreview>;
  tabbable: boolean;
  onFocus: () => void;
}) {
  const lines = { start: hit.line, end: hit.line };
  const shown = windowLine(
    hit.text,
    fromCharacterColumns(hit.text, hit.offset, hit.ranges),
    LINE_CHARS,
  );
  const start = hit.line - hit.before.length;
  const open = useOpenCode();
  const href = codePath(target.org, target.repo, hit.path, {
    ref: target.ref,
    lines: String(hit.line),
  });
  return (
    <li>
      <div className={CARD_CLASS}>
        <PreviewCard.Trigger
          handle={handle}
          payload={preview(hit, target.ref)}
          delay={400}
          closeDelay={150}
          render={
            <Link
              href={href}
              data-card=""
              tabIndex={tabbable ? 0 : -1}
              onFocus={onFocus}
              onClick={(event) => {
                open(event, href, codeViewKey(hit.path, lines) ?? "");
              }}
              className={CARD_LINK_CLASS}
            />
          }
        >
          <span className="flex min-w-0 text-xs text-fg-muted">
            <span className="min-w-0 truncate font-mono text-fg [direction:rtl]">
              <bdi>
                {hit.path}:{hit.line}
              </bdi>
            </span>
          </span>
        </PreviewCard.Trigger>
        <CodeLines
          start={start}
          className="mt-1.5 rounded-md bg-bg px-2 py-1.5 text-fg-muted"
          lines={[
            ...hit.before.map((text, index) => (
              <span key={`b${String(index)}`} className="text-fg-faint">
                {text}
              </span>
            )),
            <span key="hit" className="text-fg">
              {(shown.cutStart || hit.offset > 0) && <span className="text-fg-faint">…</span>}
              <Marked text={shown.text} ranges={shown.ranges} />
              {shown.cutEnd && <span className="text-fg-faint">…</span>}
            </span>,
            ...hit.after.map((text, index) => (
              <span key={`a${String(index)}`} className="text-fg-faint">
                {text}
              </span>
            )),
          ]}
        />
      </div>
    </li>
  );
}

/** An exact search's matches (`mode=regex`): every matching line, in path order. */
export function ExactResults({
  result,
  target,
  stale = false,
}: {
  result: GrepResult;
  target: CardTarget;
  stale?: boolean;
}) {
  const headerId = useId();
  const [handle] = useState(() => PreviewCard.createHandle<CodePreview>());
  const [active, setActive] = useState<string | null>(null);
  const key = (hit: GrepHit) => `${hit.path}:${String(hit.line)}`;
  const roving = result.hits.some((hit) => key(hit) === active)
    ? active
    : result.hits[0]
      ? key(result.hits[0])
      : null;
  return (
    <section
      aria-labelledby={headerId}
      data-lane="exact"
      aria-busy={stale}
      className={cn("flex max-w-4xl flex-col gap-2", stale && "opacity-60")}
    >
      <h3 id={headerId} className="text-xs font-medium text-fg">
        Exact matches{" "}
        <span className="text-fg-muted tabular-nums">
          {plural(result.hits.length, "line")}
          {result.truncated ? ", more not shown" : ""}
        </span>
      </h3>
      {result.timed_out && (
        <p className="text-xs text-warn">The search stopped after 3 seconds; there may be more.</p>
      )}
      <ul className="flex flex-col gap-2">
        {result.hits.map((hit) => (
          <ExactCard
            key={key(hit)}
            hit={hit}
            target={target}
            handle={handle}
            tabbable={key(hit) === roving}
            onFocus={() => {
              setActive(key(hit));
            }}
          />
        ))}
      </ul>
      <CodeHoverCard handle={handle} />
    </section>
  );
}
