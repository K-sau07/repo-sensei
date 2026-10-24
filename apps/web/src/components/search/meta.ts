/** What the search page calls things: lanes, query classes and why-matched reasons. Every one has
 * a text label as well as a color or glyph, so color is never the only signal. */
import { Braces, Network, ScanSearch, Sparkles, type LucideIcon } from "lucide-react";

import { LAYERS as LAYER_BADGES } from "@/components/primitives/layer-badge";
import type { Layer, MatchReason, QueryClass, SearchHit } from "@/lib/api/search";

export interface LaneMeta {
  label: string;
  glyph: string;
  /** The lane's color: its header's text and rule. */
  text: string;
  rule: string;
}

export const LANES: Record<Layer, LaneMeta> = {
  ui: { label: "UI", glyph: LAYER_BADGES.ui.glyph, text: "text-layer-ui", rule: "bg-layer-ui" },
  api: {
    label: "API",
    glyph: LAYER_BADGES.api.glyph,
    text: "text-layer-api",
    rule: "bg-layer-api",
  },
  svc: {
    label: "Service",
    glyph: LAYER_BADGES.svc.glyph,
    text: "text-layer-svc",
    rule: "bg-layer-svc",
  },
  db: { label: "DB", glyph: LAYER_BADGES.db.glyph, text: "text-layer-db", rule: "bg-layer-db" },
  other: { label: "Tests/Config", glyph: "○", text: "text-fg-muted", rule: "bg-fg-faint" },
};

export interface ClassMeta {
  label: string;
  /** How the query was read, in a sentence. */
  explain: string;
}

export const QUERY_CLASSES: Record<QueryClass, ClassMeta> = {
  symbol: {
    label: "symbol",
    explain: "Read as a name: definitions with this name come first, then the places it's used.",
  },
  literal: {
    label: "literal",
    explain: "Read as exact text: what's in quotes is matched as typed, case included.",
  },
  regex: {
    label: "regex",
    explain: "Read as a regular expression, matched line by line.",
  },
  conceptual: {
    label: "concept",
    explain: "Read as a concept: matched by meaning as well as by its words.",
  },
  flow: {
    label: "flow",
    explain: "Read as a flow: how something travels from the UI through the API to the data.",
  },
  architecture: {
    label: "architecture",
    explain: "Read as a question about structure: the modules and how they connect.",
  },
  impact: {
    label: "impact",
    explain: "Read as an impact question: what depends on this, and what a change would touch.",
  },
};

export const REASONS: Record<MatchReason, { label: string; explain: string; icon: LucideIcon }> = {
  keyword: { label: "keyword", explain: "The query's words are in it", icon: ScanSearch },
  semantic: { label: "semantic", explain: "Close in meaning to the query", icon: Sparkles },
  symbol: { label: "symbol", explain: "Its name matches the query", icon: Braces },
  graph: { label: "graph", explain: "Connected to another match", icon: Network },
};

const KIND_LABELS: Record<SearchHit["kind"], string> = {
  module: "module",
  class: "class",
  function: "function",
  method: "method",
  component: "component",
  hook: "hook",
  variable: "variable",
  file: "lines",
};

export const kindLabel = (kind: SearchHit["kind"]) => KIND_LABELS[kind];

export const plural = (count: number, one: string, many = `${one}s`) =>
  `${count.toLocaleString("en-US")} ${count === 1 ? one : many}`;
