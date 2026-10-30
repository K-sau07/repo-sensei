"use client";

/**
 * Where a path stops short of the data, as the engine says why: a gap (the graph is missing the
 * next hop), with what the graph knows of why and "Help me resolve" (Ask, phase 4, will open
 * with the question; until then it is copied, to paste where it helps); an end (server code
 * that does its work without the data: a DTO built, a token signed); or the trace's hop limit,
 * with a way to trace on from there.
 */
import { cn } from "cn";
import { CircleStop, MessageSquareText, MoveRight } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";

import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { hopName, resolveQuestion, type TraceBreak, type TracePath } from "@/lib/api/trace";
import { traceHref } from "@/lib/trace/url-state";

import { HopPip } from "./hop-pip";

/** Copy the gap's question, and say so (or show it, when the clipboard is out of reach). */
export async function copyQuestion(path: TracePath, gap: TraceBreak): Promise<void> {
  const question = resolveQuestion(path, gap);
  try {
    await navigator.clipboard.writeText(question);
    toast("Copied a question about this gap", {
      id: "trace-gap",
      description: "Ask arrives in phase 4; until then, paste it wherever you ask.",
    });
  } catch {
    toast("Couldn't copy the question", { id: "trace-gap", description: question });
  }
}

export function TraceGap({
  path,
  gap,
  className,
}: {
  path: TracePath;
  gap: TraceBreak;
  className?: string;
}) {
  if (gap.kind === "end") return <TraceEnd gap={gap} className={className} />;
  if (gap.kind === "limit") return <TraceLimit path={path} gap={gap} className={className} />;
  return (
    <div
      role="group"
      aria-label="Gap in the trace"
      data-testid="trace-gap"
      className={cn(
        "flex flex-col gap-1.5 rounded-[10px] border border-dashed border-line-strong bg-surface-1 px-2.5 py-2 text-xs",
        className,
      )}
    >
      <p className="flex items-center gap-1 font-medium text-fg-muted">
        <HopPip edge={null} pip="unresolved" className="-ml-1" />
        Unresolved
      </p>
      <p className="text-sm text-fg">{gap.reason}</p>
      {gap.detail && <p className="text-fg-muted">{gap.detail}</p>}
      <Button
        variant="outline"
        size="xs"
        className="self-start"
        onClick={() => void copyQuestion(path, gap)}
      >
        <MessageSquareText aria-hidden />
        Help me resolve
      </Button>
    </div>
  );
}

const QUIET =
  "flex flex-col gap-1.5 rounded-[10px] border border-line bg-surface-1 px-2.5 py-2 text-xs";

/** Where this way ends: the code here calls nothing more the graph knows of. */
function TraceEnd({ gap, className }: { gap: TraceBreak; className?: string | undefined }) {
  return (
    <div
      role="group"
      aria-label="Where this path ends"
      data-testid="trace-end"
      className={cn(QUIET, className)}
    >
      <p className="flex items-center gap-1 font-medium text-fg-muted">
        <CircleStop aria-hidden className="size-3.5" />
        Ends here
      </p>
      <p className="text-sm text-fg">{gap.reason}</p>
      <p className="text-fg-muted">
        This way doesn&apos;t reach the data: the code here calls nothing more the graph knows of.
      </p>
    </div>
  );
}

/**
 * Where the trace stops though the code goes on (its hop limit, or links less sure than it was
 * asked to show): it can be traced from here.
 */
function TraceLimit({
  path,
  gap,
  className,
}: {
  path: TracePath;
  gap: TraceBreak;
  className?: string | undefined;
}) {
  const { org, repo } = useRepoSlug();
  const hop = path.hops[gap.after_hop];
  return (
    <div
      role="group"
      aria-label="Where the trace stops this path, though the code goes on"
      data-testid="trace-limit"
      className={cn(QUIET, className)}
    >
      <p className="flex items-center gap-1 font-medium text-fg-muted">
        <MoveRight aria-hidden className="size-3.5" />
        Goes on
      </p>
      <p className="text-sm text-fg">{gap.reason}</p>
      {hop && (
        <Link
          href={traceHref(org, repo, { from: hop.node.symbol_key })}
          className="self-start text-fg-muted underline underline-offset-4 hover:text-fg"
        >
          Trace from {hopName(hop.node)}
        </Link>
      )}
    </div>
  );
}
