"use client";

/**
 * Where a path stops short of the data: the reason the engine gives (and what the graph knows of
 * why), with "Help me resolve". Ask (phase 4) will open with the question; until then it is
 * copied, to paste where it helps.
 */
import { cn } from "cn";
import { MessageSquareText } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { resolveQuestion, type TraceBreak, type TracePath } from "@/lib/api/trace";

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
