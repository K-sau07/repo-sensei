"use client";

/**
 * The constellation without WebGL: a static 2D scatter, the 3D layout seen from the 3D view's
 * starting angle. Each layer is drawn as a few `<path>`s of zero-length round-capped segments
 * (one per star size), so tens of thousands of stars stay a handful of elements. Directory links
 * are one path. The pointer picks the nearest star, as in the 3D view.
 */
import { useMemo, useRef } from "react";

import { nearest, project, reach, starScale, type Layer } from "@/lib/constellation";

import type { Hit, SceneProps } from "./constellation-scene";

const LAYER_ORDER: Layer[] = ["other", "db", "svc", "api", "ui"]; // the quietest underneath
const SIZES = [1, 1.6, 2.3]; // star size buckets
const DOT = 0.012; // the smallest star's diameter, with the picture scaled to the unit sphere
const PICK_PX = 10;
const VIEW_BOX = "-1.1 -1.1 2.2 2.2";
const STROKE: Record<Layer, string> = {
  ui: "var(--layer-ui)",
  api: "var(--layer-api)",
  svc: "var(--layer-svc)",
  db: "var(--layer-db)",
  other: "var(--text-faint)",
};

const r4 = (value: number) => Math.round(value * 10_000) / 10_000;

function bucket(size: number): number {
  const scale = starScale(size);
  return scale < 1.3 ? 0 : scale < 1.95 ? 1 : 2;
}

/** The ids of directories with at least one star in or under them. */
export function litDirs(sky: SceneProps["sky"]): Set<number> {
  const parents = new Map(sky.dirs.map((d) => [d.id, d.parent]));
  const lit = new Set<number>();
  for (const point of sky.points) {
    let current: number | null | undefined = point.dir_cluster;
    while (current !== null && current !== undefined && !lit.has(current)) {
      lit.add(current);
      current = parents.get(current);
    }
  }
  return lit;
}

export function ConstellationSvg({ sky, highlight, label, onHover, onSelect }: SceneProps) {
  const svg = useRef<SVGSVGElement>(null);
  const far = useMemo(() => reach(sky), [sky]);
  // Scaled to fill the view, like the 3D one.
  const flat = useMemo(
    () => sky.points.map((p) => project(p.x / far, p.y / far, p.z / far)),
    [sky.points, far],
  );

  const paths = useMemo(() => {
    const d: Record<string, string[]> = {};
    flat.forEach(([x, y], i) => {
      const point = sky.points[i];
      if (!point) return;
      (d[`${point.layer}:${String(bucket(point.size))}`] ??= []).push(
        `M${String(r4(x))} ${String(r4(y))}h0`,
      );
    });
    return d;
  }, [flat, sky.points]);

  const edges = useMemo(() => {
    const lit = litDirs(sky);
    const byId = new Map(sky.dirs.map((dir) => [dir.id, dir]));
    const segments: string[] = [];
    for (const dir of sky.dirs) {
      const parent = dir.parent === null ? undefined : byId.get(dir.parent);
      if (!parent || !lit.has(dir.id)) continue;
      const [ax, ay] = project(dir.x / far, dir.y / far, dir.z / far);
      const [bx, by] = project(parent.x / far, parent.y / far, parent.z / far);
      segments.push(`M${String(r4(ax))} ${String(r4(ay))}L${String(r4(bx))} ${String(r4(by))}`);
    }
    return segments.join("");
  }, [sky, far]);

  const lit = useMemo(() => {
    if (highlight === null) return "";
    return flat
      .flatMap(([x, y], i) =>
        sky.points[i]?.dir_cluster === highlight ? [`M${String(r4(x))} ${String(r4(y))}h0`] : [],
      )
      .join("");
  }, [flat, highlight, sky.points]);

  const pick = (event: { clientX: number; clientY: number }): Hit | null => {
    const box = svg.current?.getBoundingClientRect();
    if (!box || flat.length === 0) return null;
    // viewBox units to pixels (preserveAspectRatio meet: the smaller side decides).
    const scale = Math.min(box.width, box.height) / 2.2;
    const left = box.width / 2;
    const top = box.height / 2;
    const screen = new Float32Array(flat.length * 2);
    flat.forEach(([x, y], i) => {
      screen[2 * i] = left + x * scale;
      screen[2 * i + 1] = top + y * scale;
    });
    const px = event.clientX - box.left;
    const py = event.clientY - box.top;
    const index = nearest(screen, flat.length, px, py, PICK_PX);
    return index < 0 ? null : { index, x: screen[2 * index] ?? px, y: screen[2 * index + 1] ?? py };
  };

  return (
    <svg
      ref={svg}
      role="img"
      aria-label={label}
      viewBox={VIEW_BOX}
      preserveAspectRatio="xMidYMid meet"
      className="size-full touch-none"
      onPointerMove={(event) => {
        onHover(pick(event));
      }}
      onPointerLeave={() => {
        onHover(null);
      }}
      onClick={(event) => {
        onSelect(pick(event));
      }}
      data-testid="constellation-svg"
    >
      <path
        d={edges}
        fill="none"
        stroke="var(--text-faint)"
        strokeOpacity={0.25}
        strokeWidth={0.004}
      />
      {LAYER_ORDER.flatMap((layer) =>
        SIZES.map((size, b) => {
          const d = paths[`${layer}:${String(b)}`];
          return d ? (
            <path
              key={`${layer}-${String(b)}`}
              d={d.join("")}
              fill="none"
              stroke={STROKE[layer]}
              strokeWidth={DOT * size}
              strokeLinecap="round"
              data-layer={layer}
            />
          ) : null;
        }),
      )}
      {lit && (
        <path
          d={lit}
          fill="none"
          stroke="var(--signal)"
          strokeWidth={DOT * 2.4}
          strokeLinecap="round"
        />
      )}
    </svg>
  );
}
