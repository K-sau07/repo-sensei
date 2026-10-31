"use client";

/**
 * A view pinned to a commit (`?ref=<sha>`, as a permalink or a cited answer opens one) that the
 * default branch's index has moved on from (5.6): it says so, and "Re-run on latest" reads it at
 * the latest index instead.
 */
import { RotateCw } from "lucide-react";

import { StaleBadge } from "@/components/feedback/banners";
import { isCommit } from "@/lib/api/refs";

import { useViewRef } from "./use-ref";
import { useRepo } from "./use-repo";

/** Whether a view at `ref` is older than the default branch's index at `indexed`. */
export function pinnedBehind(ref: string | null, indexed: string | null | undefined): boolean {
  if (ref === null || !isCommit(ref) || !indexed) return false;
  return ref.toLowerCase() !== indexed.toLowerCase();
}

export function PinnedStale({ what }: { what: string }) {
  const [ref, setRef] = useViewRef();
  const repo = useRepo({ refetchOnMount: false });
  const indexed = repo.data?.last_indexed_sha;
  if (!pinnedBehind(ref, indexed) || !ref || !indexed || !repo.data) return null;
  return (
    <p
      className="flex flex-wrap items-center gap-2 text-xs text-fg-muted"
      data-testid="pinned-stale"
    >
      <StaleBadge>
        {what} at {ref.slice(0, 7)} · {repo.data.default_branch} is at {indexed.slice(0, 7)}
      </StaleBadge>
      <button
        type="button"
        className="flex items-center gap-1 hover:text-fg"
        onClick={() => void setRef(null)}
      >
        <RotateCw aria-hidden className="size-3" />
        Re-run on latest
      </button>
    </p>
  );
}
