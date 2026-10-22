/**
 * The mock snapshot's symbols (fixtures/symbols.json): the definitions in the files whose content
 * the mock serves (fixtures/browse-content.json), at their real lines, so opening one shows it.
 * Ranked the way the symbols endpoint ranks: an exact name, then a prefix, then a substring,
 * then trigram similarity; ties by kind, path and line.
 */
import type { SymbolHit, SymbolKind } from "@/lib/api/symbols";

import { wordSimilarity } from "./browse";
import fixture from "./fixtures/symbols.json";

export type MockSymbol = Omit<SymbolHit, "score">;

export const SYMBOLS: readonly MockSymbol[] = fixture.symbols as MockSymbol[];

export const SYMBOL_KINDS: readonly SymbolKind[] = [
  "module",
  "class",
  "function",
  "method",
  "component",
  "hook",
  "variable",
];

// Definitions people jump to most come first among equals.
const KIND_RANK: Record<SymbolKind, number> = {
  class: 0,
  component: 0,
  function: 1,
  hook: 1,
  method: 2,
  variable: 3,
  module: 4,
};

const SIMILARITY_THRESHOLD = 0.3;

function scoreOf(query: string, symbol: MockSymbol): number {
  const name = symbol.name.toLowerCase();
  const qualified = symbol.qualified_name.toLowerCase();
  if (name === query || qualified === query) return 1;
  if (name.startsWith(query)) return 0.6 + 0.3 * (query.length / name.length);
  if (name.includes(query) || qualified.includes(query)) {
    return 0.4 + 0.2 * (query.length / name.length);
  }
  const similarity = Math.max(wordSimilarity(query, name), wordSimilarity(query, qualified));
  return similarity >= SIMILARITY_THRESHOLD ? 0.4 * similarity : 0;
}

export function searchSymbols(
  query: string,
  { kind, limit }: { kind: SymbolKind | null; limit: number },
): SymbolHit[] {
  const lowered = query.toLowerCase();
  const hits: SymbolHit[] = [];
  for (const symbol of SYMBOLS) {
    if (kind && symbol.kind !== kind) continue;
    const score = scoreOf(lowered, symbol);
    if (score > 0) hits.push({ ...symbol, score: Math.round(score * 10_000) / 10_000 });
  }
  hits.sort(
    (a, b) =>
      b.score - a.score ||
      KIND_RANK[a.kind] - KIND_RANK[b.kind] ||
      (a.path < b.path ? -1 : a.path > b.path ? 1 : 0) ||
      a.start_line - b.start_line,
  );
  return hits.slice(0, limit);
}
