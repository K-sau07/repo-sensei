"use client";

/**
 * The Trace Beam (signature moment 2): one SVG path through the hops' cards, in the lanes' colors
 * (a gradient along it, each hop's color at its card). When a trace resolves (its path reaches
 * the data) a light pulse runs along it once, `--dur-signature` with `--ease-inout`, drawing the
 * path in behind it; then it rests. Only `stroke-dashoffset` and `opacity` animate. With reduced
 * motion it is the resting path from the start. A gap is a dashed line to where the next hop
 * would be. Decoration only: the hops' list carries what it shows.
 */
import { useReducedMotionConfig } from "motion/react";
import { useId, useState } from "react";

import type { TraceLayer } from "@/lib/api/trace";

import { TRACE_LANES } from "./meta";

/** Where a hop's card is in the swimlanes (px, from their top left): its sides and middle. */
export interface Anchor {
  left: number;
  right: number;
  y: number;
  layer: TraceLayer;
}

const r1 = (value: number) => String(Math.round(value * 10) / 10);

/** The beam's path: into each card from its left, out of it on its right, curving between lanes. */
export function beamPath(anchors: readonly Anchor[]): string {
  const [first, ...rest] = anchors;
  if (!first) return "";
  let d = `M${r1(first.left)} ${r1(first.y)}L${r1(first.right)} ${r1(first.y)}`;
  let previous = first;
  for (const anchor of rest) {
    const bend = Math.max((anchor.left - previous.right) / 2, 0);
    d +=
      `C${r1(previous.right + bend)} ${r1(previous.y)} ${r1(anchor.left - bend)} ${r1(anchor.y)} ` +
      `${r1(anchor.left)} ${r1(anchor.y)}L${r1(anchor.right)} ${r1(anchor.y)}`;
    previous = anchor;
  }
  return d;
}

/** The pulse's length, as a share of the path (the path is measured as 1). */
const PULSE = 0.12;

export function TraceBeam({
  anchors,
  gap,
  width,
  height,
  pulse,
  onPulsed,
}: {
  anchors: readonly Anchor[];
  /** From the hop a path breaks after to where its gap is shown. */
  gap: { from: Anchor; to: Anchor } | null;
  width: number;
  height: number;
  /** Run the pulse (once: the trace just resolved). Read when the beam mounts. */
  pulse: boolean;
  /** The pulse has run. */
  onPulsed?: () => void;
}) {
  const id = useId();
  // The app's MotionConfig ("user": the OS setting) decides, so a story can force it.
  const reduced = useReducedMotionConfig() ?? false;
  // The pulse runs once from when the beam appears, whatever its parent renders meanwhile.
  const [wanted] = useState(pulse);
  const [done, setDone] = useState(false);
  const first = anchors[0];
  const last = anchors.at(-1);
  if (!first || !last || width === 0) return null;
  const d = beamPath(anchors);
  const x1 = first.left;
  const x2 = Math.max(last.right, x1 + 1);
  const playing = wanted && !reduced && !done;
  return (
    <svg
      aria-hidden
      data-testid="trace-beam"
      data-pulse={playing ? "playing" : wanted && !reduced ? "done" : "off"}
      className="pointer-events-none absolute inset-0 overflow-visible"
      width={width}
      height={height}
      viewBox={`0 0 ${String(width)} ${String(height)}`}
    >
      <defs>
        <linearGradient
          id={`${id}-lanes`}
          gradientUnits="userSpaceOnUse"
          x1={x1}
          y1={0}
          x2={x2}
          y2={0}
        >
          {anchors.map((anchor, i) => (
            <stop
              key={i}
              offset={Math.min(Math.max(((anchor.left + anchor.right) / 2 - x1) / (x2 - x1), 0), 1)}
              style={{ stopColor: TRACE_LANES[anchor.layer].color }}
            />
          ))}
        </linearGradient>
      </defs>
      <path
        d={d}
        fill="none"
        stroke={`url(#${id}-lanes)`}
        strokeWidth={2}
        strokeLinecap="round"
        pathLength={1}
        strokeDasharray={playing ? "1 1" : undefined}
        style={
          playing
            ? { animation: "trace-beam-draw var(--dur-signature) var(--ease-inout) both" }
            : undefined
        }
        data-beam="rest"
      />
      {gap && (
        <path
          d={`M${r1(gap.from.right)} ${r1(gap.from.y)}C${r1((gap.from.right + gap.to.left) / 2)} ${r1(gap.from.y)} ${r1((gap.from.right + gap.to.left) / 2)} ${r1(gap.to.y)} ${r1(gap.to.left)} ${r1(gap.to.y)}`}
          fill="none"
          stroke="var(--text-faint)"
          strokeWidth={1.5}
          strokeDasharray="4 4"
          data-beam="gap"
        />
      )}
      {playing && (
        <path
          d={d}
          fill="none"
          stroke="var(--signal)"
          strokeWidth={3}
          strokeLinecap="round"
          pathLength={1}
          strokeDasharray={`${String(PULSE)} ${String(1 + PULSE)}`}
          style={{
            animation: "trace-beam-pulse var(--dur-signature) var(--ease-inout) both",
            filter: "drop-shadow(0 0 4px var(--signal))",
          }}
          onAnimationEnd={() => {
            setDone(true);
            onPulsed?.();
          }}
          data-beam="pulse"
        />
      )}
    </svg>
  );
}
