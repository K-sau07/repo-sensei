/**
 * The mock snapshot's symbols (fixtures/symbols.json): the definitions in the files whose content
 * the mock serves (fixtures/browse-content.json), extracted by the engine
 * (scripts/mock_symbols.py), so opening one shows it. Found and ranked as the symbols endpoint
 * does: an exact name, then a prefix, a qualified name's ending, a substring, then trigram
 * similarity; ties by name length, path and line.
 */
import type { SymbolHit, SymbolKind } from "@/lib/api/types";

import { fileAt, similarity } from "./browse";
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

// The API's ranking (sensei_engine.symbols.lookup), weight for weight.
const KIND_WEIGHT: Record<SymbolKind, number> = {
  class: 1,
  component: 1,
  function: 1,
  hook: 1,
  method: 0.95,
  module: 0.9,
  variable: 0.85,
};
const TEST_WEIGHT = 0.85;
const GENERATED_WEIGHT = 0.9;
const FUZZY_FLOOR = 0.3;
const TRIGRAM_MIN = 3;

/** Whether one of the API's searches finds the symbol: the name's prefix; the qualified name
 * (only for a query with a dot); from three characters, containing the query, or (`fuzzy`)
 * similar to it. */
function found(query: string, symbol: MockSymbol, fuzzy: boolean): boolean {
  const name = symbol.name.toLowerCase();
  const qualified = symbol.qualified_name.toLowerCase();
  if (name.startsWith(query)) return true;
  if (query.includes(".") && (qualified === query || qualified.endsWith(`.${query}`))) return true;
  if (query.length < TRIGRAM_MIN) return false;
  if (name.includes(query)) return true;
  return fuzzy && similarity(symbol.name, query) >= FUZZY_FLOOR;
}

function scoreOf(query: string, symbol: MockSymbol): number {
  const lowered = query.toLowerCase();
  const name = symbol.name.toLowerCase();
  const qualified = symbol.qualified_name.toLowerCase();
  const coverage = query.length / Math.max(symbol.name.length, 1);
  let match: number;
  if (symbol.name === query) match = 1;
  else if (name === lowered || qualified === lowered) match = 0.95;
  else if (name.startsWith(lowered)) match = 0.75 + coverage * 0.15;
  else if (qualified.endsWith(`.${lowered}`)) match = 0.7;
  else if (name.includes(lowered)) match = 0.5 + coverage * 0.2;
  else match = similarity(symbol.name, lowered) * 0.5;
  const file = fileAt(symbol.path);
  const fileWeight =
    file?.role === "test"
      ? TEST_WEIGHT
      : file?.flags.some((flag) => flag === "generated" || flag === "vendored")
        ? GENERATED_WEIGHT
        : 1;
  return match * KIND_WEIGHT[symbol.kind] * fileWeight;
}

function ranked(query: string, kind: SymbolKind | null, limit: number, fuzzy: boolean) {
  const lowered = query.toLowerCase();
  const scored: { symbol: MockSymbol; score: number }[] = [];
  for (const symbol of SYMBOLS) {
    if (kind && symbol.kind !== kind) continue;
    if (found(lowered, symbol, fuzzy)) scored.push({ symbol, score: scoreOf(query, symbol) });
  }
  // Sorted on the exact score, rounded after, as the API does; paths compare as text does in
  // the database (by locale, not code unit).
  scored.sort(
    (a, b) =>
      b.score - a.score ||
      a.symbol.name.length - b.symbol.name.length ||
      a.symbol.path.localeCompare(b.symbol.path, "en-US") ||
      a.symbol.start_line - b.symbol.start_line,
  );
  return scored.slice(0, limit).map(({ symbol, score }): SymbolHit => ({
    ...symbol,
    score: Math.round(score * 10_000) / 10_000,
  }));
}

/** The API's answer: typos are looked for only when the other matches don't fill the page. */
export function searchSymbols(
  query: string,
  { kind, limit }: { kind: SymbolKind | null; limit: number },
): SymbolHit[] {
  const hits = ranked(query, kind, limit, false);
  if (hits.length >= limit || query.length < TRIGRAM_MIN) return hits;
  return ranked(query, kind, limit, true);
}
