/**
 * The hero's sky as a picture (10.3): the same stars and Trace Beam as the WebGL scene, projected
 * from the same angle and drawn on the server, so the first paint already shows the repository
 * (and readers with reduced motion, or without WebGL, keep it). Decoration: the page says it all.
 */
import { project } from "@/lib/constellation";
import { SKY_LAYERS, type HeroSky } from "@/lib/demo/hero-sky";

const SIZE = 1000;
const SCALE = 400; // the unit sphere, in the picture's units
const COLORS: Record<string, string> = {
  ui: "var(--layer-ui)",
  api: "var(--layer-api)",
  svc: "var(--layer-svc)",
  db: "var(--layer-db)",
  other: "var(--text-faint)",
};

const at = (x: number, y: number, z: number) => {
  const [px, py] = project(x, y, z);
  return [SIZE / 2 + px * SCALE, SIZE / 2 + py * SCALE] as const;
};

export function HeroSkySvg({ sky, className }: { sky: HeroSky; className?: string }) {
  const stars = Array.from({ length: sky.points.length / 3 }, (_, i) => {
    const [x, y] = at(
      sky.points[3 * i] ?? 0,
      sky.points[3 * i + 1] ?? 0,
      sky.points[3 * i + 2] ?? 0,
    );
    return { x, y, color: COLORS[SKY_LAYERS[sky.layers[i] ?? 4] ?? "other"] ?? COLORS.other };
  });
  const links: string[] = [];
  for (let i = 0; i + 5 < sky.links.length; i += 6) {
    const [x1, y1] = at(sky.links[i] ?? 0, sky.links[i + 1] ?? 0, sky.links[i + 2] ?? 0);
    const [x2, y2] = at(sky.links[i + 3] ?? 0, sky.links[i + 4] ?? 0, sky.links[i + 5] ?? 0);
    links.push(`M${x1.toFixed(1)} ${y1.toFixed(1)}L${x2.toFixed(1)} ${y2.toFixed(1)}`);
  }
  const beam = sky.beam
    .map((stop, i) => {
      const s = stars[stop.star];
      return s ? `${i === 0 ? "M" : "L"}${s.x.toFixed(1)} ${s.y.toFixed(1)}` : "";
    })
    .join("");
  return (
    <svg
      aria-hidden
      viewBox={`0 0 ${SIZE} ${SIZE}`}
      preserveAspectRatio="xMidYMid meet"
      className={className}
    >
      <path d={links.join("")} stroke="var(--line-strong)" strokeWidth={0.6} opacity={0.45} />
      {stars.map((s, i) => (
        <circle key={i} cx={s.x.toFixed(1)} cy={s.y.toFixed(1)} r={2.4} fill={s.color} />
      ))}
      <path d={beam} fill="none" stroke="var(--signal)" strokeWidth={1.6} opacity={0.85} />
      {sky.beam.map((stop) => {
        const s = stars[stop.star];
        return s ? (
          <circle key={stop.star} cx={s.x} cy={s.y} r={5} fill="var(--signal)" opacity={0.9} />
        ) : null;
      })}
    </svg>
  );
}
