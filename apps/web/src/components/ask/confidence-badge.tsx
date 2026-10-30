"use client";

/**
 * How sure an answer is (4.11): a badge with its level, opening the breakdown the level was
 * worked out from (claims checked against the code, what was inferred, names not found).
 */
import { cn } from "cn";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { confidenceLines, type ConfidenceData } from "@/lib/api/ask";

const DOT: Record<ConfidenceData["level"], string> = {
  high: "bg-ok",
  medium: "bg-warn",
  low: "bg-danger",
};

export function ConfidenceBadge({ confidence }: { confidence: ConfidenceData }) {
  return (
    <Popover>
      <PopoverTrigger
        data-testid="confidence"
        className={cn(
          "inline-flex items-center gap-1.5 rounded-sm px-1 hover:text-fg",
          confidence.level === "low" && "text-warn",
        )}
      >
        <span aria-hidden className={cn("size-1.5 rounded-full", DOT[confidence.level])} />
        Confidence: {confidence.level}
      </PopoverTrigger>
      <PopoverContent
        aria-label="How sure the answer is"
        className="w-80 p-3 text-xs"
        data-testid="confidence-breakdown"
      >
        <ConfidenceBreakdown confidence={confidence} />
      </PopoverContent>
    </Popover>
  );
}

export function ConfidenceBreakdown({ confidence }: { confidence: ConfidenceData }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="font-medium text-fg">
        {confidence.level[0]?.toUpperCase()}
        {confidence.level.slice(1)} confidence
        <span className="text-fg-faint"> · {Math.round(confidence.score * 100)}%</span>
      </p>
      <ul className="flex list-disc flex-col gap-1 pl-4 text-fg-muted">
        {confidenceLines(confidence.breakdown).map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </div>
  );
}
