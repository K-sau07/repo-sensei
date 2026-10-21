/** Indexing stages and the features they unlock, in people's words. The unlock map itself comes
 * from the API (`GET /api/v1/indexing/stages`), so a feature appears exactly when its stage is
 * done. */
import type { Schemas, Stage } from "@/lib/api/types";

export type Feature = Schemas["Feature"];

export const STAGE_LABELS: Record<Stage, string> = {
  clone: "Clone",
  discover: "File discovery",
  merkle: "Change tracking",
  parse: "Parsing",
  symbols: "Symbols",
  chunks: "Keyword index",
  embeddings: "Embeddings",
  summaries: "Summaries",
  graph: "Code graph",
  links: "Cross-layer links",
  finalize: "Finishing up",
};

export const FEATURE_LABELS: Record<Feature, string> = {
  code_browser: "Code browser",
  goto_definition: "Go to definition",
  keyword_search: "Keyword search",
  semantic_search: "Semantic search",
  ask: "Ask",
  wiki: "Wiki",
  graph_view: "Graph",
  callers: "Callers",
  trace: "Trace",
  diagrams: "Diagrams",
  fixes: "Fixes",
  changes: "Changes",
};
