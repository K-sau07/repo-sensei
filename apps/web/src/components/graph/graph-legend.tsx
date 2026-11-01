/** What the graph's colors mean: a node's layer (each with its glyph, so color is never the only
 * signal), and the red of an edge that closes a cycle. */
import { cn } from "cn";

import { LAYERS } from "@/components/primitives/layer-badge";
import type { Tint } from "@/lib/api/deps";

const SWATCH: Record<Tint, string> = {
  ui: "bg-layer-ui",
  api: "bg-layer-api",
  svc: "bg-layer-svc",
  db: "bg-layer-db",
  other: "bg-fg-faint",
};

const ENTRIES: { tint: Tint; label: string; glyph: string }[] = [
  ...(["ui", "api", "svc", "db"] as const).map((tint) => ({
    tint,
    label: LAYERS[tint].label,
    glyph: LAYERS[tint].glyph,
  })),
  { tint: "other", label: "Other", glyph: "·" },
];

export function GraphLegend({ className }: { className?: string }) {
  return (
    <ul
      aria-label="Legend"
      className={cn(
        "flex flex-wrap items-center gap-x-3 gap-y-1 rounded-sm border border-line bg-surface-1/90 px-2 py-1 text-xs text-fg-muted",
        className,
      )}
    >
      {ENTRIES.map(({ tint, label, glyph }) => (
        <li key={tint} className="flex items-center gap-1">
          <span aria-hidden className={cn("size-2 rounded-full", SWATCH[tint])} />
          <span aria-hidden>{glyph}</span>
          {label}
        </li>
      ))}
      <li className="flex items-center gap-1">
        <span aria-hidden className="h-0.5 w-3 bg-danger" />
        Closes a cycle
      </li>
      <li className="text-fg-faint">Size: depended on</li>
    </ul>
  );
}
