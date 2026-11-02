"use client";

/** What a pasted trace parses to, shown as it is typed (8.11): the error, and each frame as a
 * `file:line` chip, the repository's own opening the code viewer at that line. */
import { cn } from "cn";
import { FileCode2 } from "lucide-react";
import Link from "next/link";

import { useRepoSlug } from "@/components/repo/use-repo";
import { frameHref, frameLabel, type Ticket } from "@/lib/api/fixes";

export function TracePreview({ ticket, className }: { ticket: Ticket; className?: string }) {
  const { org, repo } = useRepoSlug();
  const frames = ticket.frames;
  const inRepo = frames.filter((frame) => frame.in_repo).length;
  return (
    <section
      aria-label="What the trace says"
      data-testid="trace-preview"
      className={cn("flex flex-col gap-3 rounded-md border border-line p-4", className)}
    >
      <div className="flex flex-col gap-0.5">
        <p className="text-sm font-medium">
          {ticket.error_type ? (
            <>
              <span className="font-mono">{ticket.error_type}</span>
              {ticket.message ? `: ${ticket.message}` : ""}
            </>
          ) : (
            "No error type found: the text is kept as the report."
          )}
        </p>
        <p className="text-xs text-fg-muted">
          {frames.length === 0
            ? "No stack frames found."
            : `${String(frames.length)} frame${frames.length === 1 ? "" : "s"}, ${String(inRepo)} in this repository.`}
          {Object.entries(ticket.versions).map(([name, version]) => ` · ${name} ${version}`)}
        </p>
      </div>
      {frames.length > 0 && (
        <ol className="flex flex-wrap gap-1.5" aria-label="Frames, outermost first">
          {frames.map((frame, i) => {
            const href = frameHref(org, repo, frame);
            const culprit =
              ticket.culprit !== null &&
              frame.file === ticket.culprit.file &&
              frame.line === ticket.culprit.line;
            const chip = cn(
              "inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 font-mono text-xs",
              culprit ? "border-warn/60 text-fg" : "border-line text-fg-muted",
              !frame.in_repo && "border-dashed",
            );
            const label = (
              <>
                {frame.in_repo && <FileCode2 aria-hidden className="size-3" />}
                {frameLabel(frame)}
                {frame.function && <span className="text-fg-faint"> in {frame.function}</span>}
                {culprit && (
                  <span className="sr-only"> (the innermost frame in the repository)</span>
                )}
              </>
            );
            return (
              <li key={`${frame.file}:${String(frame.line)}:${String(i)}`}>
                {href ? (
                  <Link
                    href={href}
                    className={cn(chip, "hover:bg-surface-2")}
                    data-testid="frame-chip"
                  >
                    {label}
                  </Link>
                ) : (
                  <span
                    className={chip}
                    title={frame.library ? "A library's frame" : "Not a file of this repository"}
                  >
                    {label}
                  </span>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
