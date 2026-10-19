import { cn } from "cn";

export type Layer = "ui" | "api" | "svc" | "db";

/** Each layer has a glyph as well as a color, so color is never the only signal. */
export const LAYERS: Record<Layer, { label: string; glyph: string; className: string }> = {
  ui: { label: "UI", glyph: "▢", className: "text-layer-ui bg-layer-ui/12 border-layer-ui/30" },
  api: {
    label: "API",
    glyph: "⇄",
    className: "text-layer-api bg-layer-api/12 border-layer-api/30",
  },
  svc: {
    label: "Service",
    glyph: "◇",
    className: "text-layer-svc bg-layer-svc/12 border-layer-svc/30",
  },
  db: { label: "Data", glyph: "⛁", className: "text-layer-db bg-layer-db/12 border-layer-db/30" },
};

interface LayerBadgeProps {
  layer: Layer;
  /** Hide the text label (the glyph stays and the label moves to aria-label). */
  compact?: boolean;
  className?: string;
}

export function LayerBadge({ layer, compact = false, className }: LayerBadgeProps) {
  const { label, glyph, className: tone } = LAYERS[layer];
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center gap-1 rounded-sm border px-1.5 text-xs font-medium",
        tone,
        className,
      )}
      {...(compact ? { role: "img", "aria-label": `${label} layer` } : {})}
    >
      <span aria-hidden>{glyph}</span>
      {!compact && label}
    </span>
  );
}
