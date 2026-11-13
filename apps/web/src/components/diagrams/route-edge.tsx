"use client";

/**
 * An architecture's line (6.6), drawn along the route ELK found for it (square, round the boxes,
 * its corners rounded), its label on its longest straight run; a square step line from handle to
 * handle until the route is known. A line is wider to click than it looks.
 */
import {
  BaseEdge,
  EdgeLabelRenderer,
  getSmoothStepPath,
  type Edge,
  type EdgeProps,
} from "@xyflow/react";

import type { Placed } from "@/lib/diagrams/elk-layout";
import { labelPoint, roundedPath } from "@/lib/diagrams/system-layout";

export interface RouteData extends Record<string, unknown> {
  points: Placed[];
  label?: string | undefined;
  verified: boolean;
}

export function RouteEdge(props: EdgeProps<Edge<RouteData>>) {
  const { data, style, markerEnd, sourceX, sourceY, targetX, targetY } = props;
  const points = data?.points ?? [];
  const [fallback, fx, fy] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition: props.sourcePosition,
    targetPosition: props.targetPosition,
    borderRadius: 10,
  });
  const routed = points.length >= 2;
  const path = routed ? roundedPath(points) : fallback;
  const at = routed ? labelPoint(points) : { x: fx, y: fy };
  return (
    <>
      <BaseEdge
        id={props.id}
        path={path}
        style={style}
        {...(markerEnd ? { markerEnd } : {})}
        interactionWidth={18}
      />
      {data?.label ? (
        <EdgeLabelRenderer>
          <span
            className="pointer-events-none absolute rounded-sm bg-surface-1 px-1 text-[10px] whitespace-nowrap text-fg-muted"
            style={{
              transform: `translate(-50%, -50%) translate(${String(at.x)}px, ${String(at.y)}px)`,
              opacity: typeof style?.opacity === "number" ? style.opacity : 1,
            }}
          >
            {data.label}
          </span>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}
