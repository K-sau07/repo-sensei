/** A fix's or a change's stages as a rail (8.12, 9.3): done, the one it is at (and how that's
 * going), and those to come. */
import { cn } from "cn";
import { Check, Circle, CircleDot, X } from "lucide-react";

import { CHANGE_STAGES } from "@/lib/api/changes";
import { STAGES, STAGE_LABELS, isOpen, type FixStage, type FixStatus } from "@/lib/api/fixes";

export function RunStages({
  kind,
  stage,
  status,
}: {
  kind: "fix" | "change";
  stage: FixStage;
  status: FixStatus;
}) {
  const stages = (kind === "change" ? CHANGE_STAGES : STAGES).filter((s) => s !== "done");
  const at = stages.indexOf(stage === "done" ? "pr" : stage);
  const finished = status === "succeeded";
  const stopped = !finished && !isOpen(status);
  return (
    <ol
      className="flex flex-wrap items-center gap-1 text-xs"
      aria-label="Stages"
      data-testid="run-stages"
    >
      {stages.map((s, i) => {
        const done = finished || i < at;
        const current = !finished && i === at;
        const state = done ? "done" : current ? (stopped ? "stopped" : "current") : "to come";
        return (
          <li
            key={s}
            data-state={state}
            aria-current={current ? "step" : undefined}
            className={cn(
              "flex items-center gap-1 rounded-full border px-2 py-0.5",
              done && "border-ok/40 text-ok",
              current && !stopped && "border-signal/50 text-fg",
              current && stopped && "border-danger/50 text-danger",
              !done && !current && "border-line text-fg-muted",
            )}
          >
            {done ? (
              <Check aria-hidden className="size-3" />
            ) : current ? (
              stopped ? (
                <X aria-hidden className="size-3" />
              ) : (
                <CircleDot aria-hidden className="size-3" />
              )
            ) : (
              <Circle aria-hidden className="size-3" />
            )}
            {STAGE_LABELS[s]}
            <span className="sr-only"> ({state})</span>
          </li>
        );
      })}
    </ol>
  );
}
