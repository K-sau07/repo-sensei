import { cn } from "cn";

import { LiveDot } from "@/components/primitives/live-dot";
import type { Repo } from "@/lib/api/types";

type StatusFields = Pick<Repo, "status" | "active_job">;

/** An incremental run's progress, as a whole percent (null: no incremental run going). A push
 * reads again only what changed, so the repository stays readable at its last index meanwhile. */
export function updatePercent(repo: StatusFields): number | null {
  const job = repo.active_job;
  if (job?.kind !== "incremental") return null;
  return job.status === "running" ? Math.round(job.progress * 100) : 0;
}

/** What a repository's status means to a person, and how it looks. */
export function describeStatus(repo: StatusFields): { label: string; tone: string; live: boolean } {
  const job = repo.active_job;
  if (job?.kind === "incremental") {
    const on = job.ref ? ` ${job.ref}` : "";
    return job.status === "queued"
      ? { label: `Update${on} queued`, tone: "text-fg-muted", live: true }
      : { label: `Updating${on} ${String(updatePercent(repo))}%`, tone: "text-fg", live: true };
  }
  if (job) {
    const percent = Math.round(job.progress * 100);
    return job.status === "queued"
      ? { label: "Queued for indexing", tone: "text-fg-muted", live: true }
      : { label: `Indexing ${percent}%`, tone: "text-fg", live: true };
  }
  switch (repo.status) {
    case "ready":
      return { label: "Ready", tone: "text-ok", live: false };
    case "error":
      return { label: "Indexing failed", tone: "text-danger", live: false };
    case "access_lost":
      return { label: "Access lost", tone: "text-warn", live: false };
    case "archived":
      return { label: "Archived", tone: "text-fg-faint", live: false };
    case "indexing":
      return { label: "Indexing", tone: "text-fg", live: true };
    default:
      return { label: "Not indexed yet", tone: "text-fg-faint", live: false };
  }
}

const GLYPH: Record<string, string> = {
  ready: "●",
  error: "✕",
  access_lost: "✕",
  archived: "○",
  pending: "○",
};

/** A status dot (●, live while indexing, ◌ while a push's incremental run reads what changed,
 * ✕ for problems) and, optionally, its label. Never color alone: the glyph and the label say it
 * too. */
export function RepoStatus({
  repo,
  showLabel = false,
  className,
}: {
  repo: StatusFields;
  showLabel?: boolean;
  className?: string;
}) {
  const { label, tone, live } = describeStatus(repo);
  const updating = updatePercent(repo) !== null;
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs", tone, className)}>
      {updating ? (
        <span
          aria-hidden
          className="w-2 text-center text-[0.7rem] leading-none text-signal motion-safe:animate-pulse"
          data-testid="update-dot"
        >
          ◌
        </span>
      ) : live ? (
        // With the label on screen, the dot's own text would make readers say it twice.
        <LiveDot label={showLabel ? "" : label} />
      ) : (
        <span aria-hidden className="w-2 text-center text-[0.6rem] leading-none">
          {GLYPH[repo.status] ?? "○"}
        </span>
      )}
      {showLabel ? (
        <span className="tabular-nums">{label}</span>
      ) : (
        (!live || updating) && <span className="sr-only">{label}</span>
      )}
    </span>
  );
}
