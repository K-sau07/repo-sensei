"use client";

/**
 * An answer the run couldn't stand behind (4.11): it says so instead of guessing. Why, what
 * it searched, where the trail it followed ended (a gap after the last step it could trace),
 * and a narrower question to ask instead.
 */
import { CircleSlash } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { AbstainData } from "@/lib/api/ask";

export function AbstainCard({
  abstain,
  onAsk,
}: {
  abstain: AbstainData;
  onAsk?: ((question: string) => void) | undefined;
}) {
  const end = abstain.trailEnd;
  return (
    <div
      className="flex flex-col gap-3 rounded-md border border-warn/40 bg-warn/5 p-4 text-sm"
      data-testid="abstain-card"
    >
      <p className="flex items-center gap-2 font-medium">
        <CircleSlash aria-hidden className="size-4 text-warn" />I couldn&apos;t verify this
      </p>
      <p className="text-fg-muted">{abstain.reason}</p>
      {end?.end ? (
        <div className="flex items-center gap-2 text-xs" aria-label="Where the trail ended">
          <span className="rounded-sm border border-border bg-muted px-1.5 py-0.5 font-mono">
            {end.end}
          </span>
          <span aria-hidden className="h-px w-10 border-t border-dashed border-fg-faint" />
          <span className="text-fg-faint">{end.reason ?? "the trail ends here"}</span>
        </div>
      ) : null}
      {abstain.searched.length ? (
        <details className="text-xs text-fg-muted">
          <summary className="cursor-pointer">Searched ({abstain.searched.length})</summary>
          <ul className="mt-1 list-disc pl-5 font-mono">
            {abstain.searched.map((query, i) => (
              <li key={`${i}-${query}`}>{query}</li>
            ))}
          </ul>
        </details>
      ) : null}
      {abstain.suggestion && onAsk ? (
        <div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              onAsk(abstain.suggestion);
            }}
          >
            Ask: {abstain.suggestion}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
