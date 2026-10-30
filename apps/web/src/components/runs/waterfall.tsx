"use client";

/**
 * A run's waterfall (4.12): each step a bar, from when it started into the run for as long as
 * it took; calls made at once side by side, on rows of their own. Steps past the one replayed
 * to are ghosted.
 */
import { cn } from "cn";

import { bar, formatMs, lanes, spanOf, stepTitle, type Step } from "@/lib/api/runs";

const ROW_PX = 14;
const KIND_COLOUR: Record<string, string> = {
  llm: "bg-brand",
  tool: "bg-layer-svc",
  retrieval: "bg-layer-api",
  verify: "bg-ok",
};

export function RunWaterfall({
  steps,
  latencyMs,
  selected,
  until,
  onSelect,
}: {
  steps: readonly Step[];
  latencyMs: number | null;
  selected: number | null;
  /** The step replayed to: those after it are ghosted. */
  until: number;
  onSelect: (idx: number) => void;
}) {
  const span = spanOf(steps, latencyMs);
  const rows = lanes(steps);
  const height = (Math.max(0, ...rows.values()) + 1) * ROW_PX;
  return (
    <div className="flex flex-col gap-1" data-testid="waterfall">
      <div
        role="group"
        aria-label="Steps over time"
        className="relative w-full overflow-hidden rounded-sm bg-surface-2"
        style={{ height }}
      >
        {steps.map((step) => {
          const { left, width } = bar(step, span);
          const row = rows.get(step.idx) ?? 0;
          return (
            <button
              key={step.idx}
              type="button"
              aria-label={`${String(step.idx + 1)}. ${stepTitle(step)}, ${formatMs(step.ms)}`}
              aria-pressed={selected === step.idx}
              onClick={() => {
                onSelect(step.idx);
              }}
              className={cn(
                "absolute h-2.5 rounded-[2px] transition-opacity hover:brightness-125",
                KIND_COLOUR[step.kind] ?? "bg-fg-muted",
                step.idx > until && "opacity-20",
                selected === step.idx && "ring-2 ring-fg",
              )}
              style={{
                left: `${String(left * 100)}%`,
                width: `${String(width * 100)}%`,
                top: row * ROW_PX + 2,
              }}
            />
          );
        })}
      </div>
      <div className="flex justify-between font-mono text-[10px] text-fg-faint">
        <span>0</span>
        <span>{formatMs(Math.round(span / 2))}</span>
        <span>{formatMs(span)}</span>
      </div>
    </div>
  );
}
