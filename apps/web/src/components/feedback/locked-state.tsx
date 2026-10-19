import { cn } from "cn";
import { Hourglass } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";

import { LiveDot } from "@/components/primitives/live-dot";

interface LockedStateProps {
  /** The feature that is waiting, e.g. "Trace". */
  feature: string;
  /** The indexing stage it needs, e.g. "Cross-layer links". */
  stage: string;
  /** Seconds until the stage is expected to finish, if known. */
  etaSeconds?: number;
  /** Where to watch indexing progress. */
  href: Route;
  className?: string;
}

export function formatEta(seconds: number): string {
  if (seconds < 60) return "under a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

/** A feature that unlocks when its indexing stage finishes. */
export function LockedState({ feature, stage, etaSeconds, href, className }: LockedStateProps) {
  return (
    <section
      aria-label={`${feature} is not ready yet`}
      className={cn(
        "flex flex-col items-center gap-3 rounded-xl border border-line p-10 text-center",
        className,
      )}
    >
      <Hourglass aria-hidden className="size-8 text-fg-faint" />
      <h2 className="text-base font-semibold">{feature} is almost ready</h2>
      <p className="flex items-center gap-2 text-sm text-fg-muted">
        <LiveDot label="In progress" />
        <span>
          Available when <span className="font-medium text-fg">{stage}</span> finishes
          {etaSeconds !== undefined && (
            <span className="tabular-nums"> (ETA {formatEta(etaSeconds)})</span>
          )}
        </span>
      </p>
      <Link href={href} className="text-sm text-fg underline underline-offset-4">
        Watch indexing
      </Link>
    </section>
  );
}
