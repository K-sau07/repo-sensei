/**
 * A dim, static constellation behind the sign-in card (the live, animated one arrives with
 * indexing). Points come from a fixed seed, so server and client render the same picture.
 */

const WIDTH = 1200;
const HEIGHT = 800;
const POINTS = 70;
const LINK_DISTANCE = 140;

function seededRandom(seed: number) {
  let state = seed;
  return () => {
    // mulberry32
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const random = seededRandom(20261019);
const points = Array.from({ length: POINTS }, () => ({
  x: Math.round(random() * WIDTH),
  y: Math.round(random() * HEIGHT),
  r: 0.8 + random() * 1.6,
}));
const links = points.flatMap((a, i) =>
  points
    .slice(i + 1)
    .filter((b) => Math.hypot(a.x - b.x, a.y - b.y) < LINK_DISTANCE)
    .map((b) => ({ a, b })),
);

export function ConstellationBackdrop() {
  return (
    <svg
      aria-hidden
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="xMidYMid slice"
      className="pointer-events-none absolute inset-0 size-full text-signal opacity-[0.18]"
    >
      {links.map(({ a, b }) => (
        <line
          key={`${a.x},${a.y}-${b.x},${b.y}`}
          x1={a.x}
          y1={a.y}
          x2={b.x}
          y2={b.y}
          stroke="currentColor"
          strokeWidth={0.6}
          strokeOpacity={0.5}
        />
      ))}
      {points.map((p) => (
        <circle key={`${p.x},${p.y}`} cx={p.x} cy={p.y} r={p.r} fill="currentColor" />
      ))}
    </svg>
  );
}
