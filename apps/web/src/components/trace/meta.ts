/** What the trace view calls things: its lanes and its pips. Every one has a text label and a
 * glyph as well as a color, so color is never the only signal. */
import { LAYERS as LAYER_BADGES } from "@/components/primitives/layer-badge";
import type { Pip, TraceLayer } from "@/lib/api/trace";

export interface TraceLaneMeta {
  label: string;
  glyph: string;
  /** The lane's color, as text, as a tint, and as a CSS value (the beam's). */
  text: string;
  band: string;
  color: string;
}

const UI = { text: "text-layer-ui", band: "bg-layer-ui/6", color: "var(--layer-ui)" };
const API = { text: "text-layer-api", band: "bg-layer-api/6", color: "var(--layer-api)" };

/** The swimlanes as a request travels them: the UI and its hooks, the call over HTTP and the
 * endpoint it reaches, the code behind it, the data. */
export const TRACE_LANES: Record<TraceLayer, TraceLaneMeta> = {
  ui: { label: "Component", glyph: LAYER_BADGES.ui.glyph, ...UI },
  state: { label: "Hook / client", glyph: "↻", ...UI },
  client: { label: "HTTP call", glyph: "↗", ...API },
  route: { label: "Endpoint", glyph: LAYER_BADGES.api.glyph, ...API },
  handler: { label: "Handler", glyph: "⌁", ...API },
  service: {
    label: "Service",
    glyph: LAYER_BADGES.svc.glyph,
    text: "text-layer-svc",
    band: "bg-layer-svc/6",
    color: "var(--layer-svc)",
  },
  db: {
    label: "Data",
    glyph: LAYER_BADGES.db.glyph,
    text: "text-layer-db",
    band: "bg-layer-db/6",
    color: "var(--layer-db)",
  },
  other: {
    label: "Other",
    glyph: "○",
    text: "text-fg-muted",
    band: "bg-fg-faint/6",
    color: "var(--text-faint)",
  },
};

export interface PipMeta {
  glyph: string;
  label: string;
  description: string;
  text: string;
}

export const PIPS: Record<Pip, PipMeta> = {
  verified: {
    glyph: "●",
    label: "Verified",
    description: "Resolved from the code itself: an import's binding, a contract, a route.",
    text: "text-ok",
  },
  inferred: {
    glyph: "◐",
    label: "Inferred",
    description: "Matched by a rule (a URL, a receiver's type, a unique name): usually right.",
    text: "text-warn",
  },
  unresolved: {
    glyph: "○",
    label: "Unresolved",
    description: "The graph has no next hop here.",
    text: "text-fg-muted",
  },
};

/** What a node is, in a word or two ("component", "HTTP call", "table"). */
export function kindLabel(kind: string): string {
  if (kind === "http_client_call") return "HTTP call";
  if (kind === "api_endpoint") return "endpoint";
  return kind.replaceAll("_", " ");
}
