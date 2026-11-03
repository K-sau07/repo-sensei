/** What depends on the code a change touches, from the graph (9.3): a mini-graph from the change
 * to each kind of dependant (routes, screens, tests, tables, other code), sized by how many, and
 * the nearest of each. */
import type { Blast, BlastItem } from "@/lib/api/changes";

const KINDS = [
  ["routes", "Routes"],
  ["screens", "Screens"],
  ["tables", "Tables"],
  ["tests", "Tests"],
  ["symbols", "Other code"],
] as const;
const WIDTH = 320;
const ROW = 28;

export function BlastRadius({ blast }: { blast: Blast | null }) {
  if (!blast) {
    return (
      <p className="text-xs text-fg-muted" data-testid="blast-radius">
        No graph for this commit yet: what depends on the change isn&apos;t known.
      </p>
    );
  }
  const rows = KINDS.map(([key, label]) => ({ key, label, count: blast.counts[key] ?? 0 }));
  const most = Math.max(1, ...rows.map((r) => r.count));
  const total = rows.reduce((sum, r) => sum + r.count, 0);
  const said = rows.map((r) => `${String(r.count)} ${r.label.toLowerCase()}`).join(", ");
  const height = rows.length * ROW;
  return (
    <div className="flex flex-col gap-3" data-testid="blast-radius">
      <svg
        viewBox={`0 0 ${String(WIDTH)} ${String(height)}`}
        className="h-auto w-full max-w-sm"
        role="img"
        aria-label={`What depends on the change: ${said}${blast.truncated ? " (and more)" : ""}.`}
      >
        <circle cx={14} cy={height / 2} r={9} className="fill-signal/30 stroke-signal" />
        {rows.map((row, i) => {
          const y = i * ROW + ROW / 2;
          const bar = row.count === 0 ? 0 : 8 + (row.count / most) * 120;
          return (
            <g key={row.key}>
              <line
                x1={23}
                y1={height / 2}
                x2={110}
                y2={y}
                className={row.count ? "stroke-fg-muted" : "stroke-line"}
                strokeWidth={row.count ? 1.5 : 1}
              />
              <text x={114} y={y + 4} className="fill-fg text-[11px]">
                {row.label}
              </text>
              <rect x={190} y={y - 6} width={bar} height={12} rx={3} className="fill-signal/40" />
              <text x={196 + bar} y={y + 4} className="fill-fg-muted text-[11px] tabular-nums">
                {row.count}
              </text>
            </g>
          );
        })}
      </svg>
      {total === 0 ? (
        <p className="text-xs text-fg-muted">
          Nothing in the graph depends on the files it changes.
        </p>
      ) : (
        <ul className="flex flex-col gap-1 text-xs" aria-label="Nearest dependants">
          {KINDS.flatMap(([key, label]) =>
            (blast.top[key] ?? []).slice(0, 3).map((item: BlastItem) => (
              <li key={`${key}:${item.key}`} className="flex gap-2">
                <span className="w-20 shrink-0 text-fg-muted">{label}</span>
                <span className="truncate font-mono">{item.name}</span>
                {item.path && <span className="truncate text-fg-faint">{item.path}</span>}
              </li>
            )),
          )}
        </ul>
      )}
    </div>
  );
}
