"use client";

/**
 * A diagram's node, drawn as on a blueprint (6.6): its layer's glyph and color (with its name:
 * never color alone), its label, and its file and lines as a chip. A node a regenerated diagram
 * added is ringed; the one picked, outlined.
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
}

export function laneOf(layer: string) {
  return TRACE_LANES[(layer in TRACE_LANES ? layer : "other") as TraceLayer];
}

export function BlueprintCard({
  node,
  added = false,
  selected = false,
}: {
  node: SpecNode;
  added?: boolean;
  selected?: boolean;
}) {
  const lane = laneOf(node.layer);
  const cited = citationLabel(node.citation);
  return (
    <div
      className={cn(
        "flex h-full w-full cursor-pointer flex-col justify-center gap-1 rounded-md border bg-surface-1/95 px-3 py-2 shadow-sm",
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
        {added ? <span className="ml-auto text-ok normal-case">new</span> : null}
      </span>
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
  const { node, added, vertical } = data;
  return (
    <div style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}>
      <Handle
        type="target"
        position={vertical ? Position.Top : Position.Left}
        className="opacity-0"
      />
      <BlueprintCard node={node} added={added} selected={selected} />
      <Handle
        type="source"
        position={vertical ? Position.Bottom : Position.Right}
        className="opacity-0"
      />
    </div>
  );
}
