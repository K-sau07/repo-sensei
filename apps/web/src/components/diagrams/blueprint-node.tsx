"use client";

/**
 * A diagram's node, drawn as on a blueprint (6.6): its layer's glyph and color (with its name:
 * never color alone), its label, and its file and lines as a chip. In a flowchart, its step's
 * number, and where the flow starts and ends. A node a regenerated diagram added is ringed; the
 * one picked, outlined.
 */
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { cn } from "cn";

import { TRACE_LANES } from "@/components/trace/meta";
import { citationLabel, type SpecNode } from "@/lib/api/diagrams";
import type { TraceLayer } from "@/lib/api/trace";
import { NODE_HEIGHT, NODE_WIDTH } from "@/lib/diagrams/elk-layout";

export interface BlueprintData extends Record<string, unknown> {
  node: SpecNode;
  added: boolean;
  vertical: boolean;
  step?: number | undefined; // a flowchart's, from 1
  end?: "start" | "end" | undefined;
}

export function laneOf(layer: string) {
  return TRACE_LANES[(layer in TRACE_LANES ? layer : "other") as TraceLayer];
}

export function BlueprintCard({
  node,
  added = false,
  selected = false,
  step,
  end,
}: {
  node: SpecNode;
  added?: boolean;
  selected?: boolean;
  step?: number | undefined;
  end?: "start" | "end" | undefined;
}) {
  const lane = laneOf(node.layer);
  const cited = citationLabel(node.citation);
  return (
    <div
      className={cn(
        "relative flex h-full w-full cursor-pointer flex-col justify-center gap-1 rounded-md border bg-surface-1/95 px-3 py-2 shadow-sm",
        end && "rounded-full px-5",
        "border-line-strong hover:border-fg-muted",
        selected && "border-fg ring-2 ring-fg/20",
        added && "ring-2 ring-ok/60",
      )}
      style={{ borderLeft: `3px solid ${lane.color}` }}
      data-testid="blueprint-node"
      data-added={added ? "true" : undefined}
    >
      <span
        className={cn("flex items-center gap-1.5 text-[10px] tracking-wide uppercase", lane.text)}
      >
        <span aria-hidden>{lane.glyph}</span>
        {lane.label}
        {end ? (
          <span className="rounded-sm bg-surface-3 px-1 text-fg-muted normal-case">
            {end === "start" ? "Start" : "End"}
          </span>
        ) : null}
        {added ? <span className="ml-auto text-ok normal-case">new</span> : null}
      </span>
      {step !== undefined ? (
        <span className="absolute -top-2.5 -left-2.5 flex size-5 items-center justify-center rounded-full border border-line-strong bg-surface-3 text-[10px] font-semibold text-fg tabular-nums">
          <span className="sr-only">Step </span>
          {step}
        </span>
      ) : null}
      <span className="truncate text-sm font-medium text-fg" title={node.id}>
        {node.label}
      </span>
      {cited ? (
        <span className="w-fit max-w-full truncate rounded-sm bg-surface-3 px-1 font-mono text-[10px] text-fg-muted">
          {cited}
        </span>
      ) : null}
    </div>
  );
}

export function BlueprintNode({ data, selected }: NodeProps<Node<BlueprintData>>) {
  const { node, added, vertical, step, end } = data;
  return (
    <div style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}>
      <Handle
        type="target"
        position={vertical ? Position.Top : Position.Left}
        className="opacity-0"
      />
      <BlueprintCard node={node} added={added} selected={selected} step={step} end={end} />
      <Handle
        type="source"
        position={vertical ? Position.Bottom : Position.Right}
        className="opacity-0"
      />
    </div>
  );
}
