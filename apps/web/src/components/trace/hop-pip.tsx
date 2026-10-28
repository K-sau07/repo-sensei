"use client";

/**
 * A hop's confidence pip: verified ●, inferred ◐ or unresolved ○ (a glyph and a word, not only a
 * color). Hover or focus says how sure, and how that's known.
 */
import { cn } from "cn";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { percent, pipOf, type Pip, type TraceEdge } from "@/lib/api/trace";

import { PIPS } from "./meta";

/** How sure, in words: "Verified, 90% sure (import resolved)". */
export function pipLabel(pip: Pip, edge: TraceEdge | null): string {
  const { label } = PIPS[pip];
  if (!edge) return label;
  return `${label}, ${percent(edge.confidence)} sure (${edge.provenance.replaceAll("_", " ")})`;
}

export function HopPip({
  edge,
  pip = pipOf(edge),
  className,
}: {
  edge: TraceEdge | null;
  /** The edge's own by default; "unresolved" for a gap. */
  pip?: Pip | null;
  className?: string;
}) {
  if (!pip) return null;
  const { glyph, description, text } = PIPS[pip];
  const label = pipLabel(pip, edge);
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            aria-label={label}
            data-pip={pip}
            className={cn(
              "inline-flex size-6 shrink-0 items-center justify-center rounded-sm text-sm leading-none outline-none hover:bg-surface-3 focus-visible:ring-2 focus-visible:ring-accent",
              text,
              className,
            )}
          />
        }
      >
        <span aria-hidden>{glyph}</span>
      </TooltipTrigger>
      <TooltipContent>
        <span className="block font-medium">{label}</span>
        <span className="block opacity-80">{description}</span>
      </TooltipContent>
    </Tooltip>
  );
}
