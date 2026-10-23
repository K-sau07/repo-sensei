"use client";

import { useEffect, useState } from "react";

import { sharedFuzzyMatcher, type FuzzyMatcher } from "@/lib/palette/fuzzy-client";
import { matchItems, type ItemMatch } from "@/lib/palette/match";
import type { Matchable } from "@/lib/palette/types";

interface Answer {
  query: string;
  items: readonly Matchable[];
  matches: ItemMatch[];
}

/**
 * What to show while the worker works on `query`: the previous answer's rows that still match
 * it, matched again here (a handful, so it's cheap), so rows don't blink and ↵ can never pick one
 * that doesn't match what's typed.
 */
export function interimMatches(answer: Answer, query: string): ItemMatch[] {
  if (answer.query === query) return answer.matches;
  const shown = new Set(answer.matches.map((match) => match.id));
  return matchItems(
    query,
    answer.items.filter((item) => shown.has(item.id)),
  );
}

/**
 * `items` matched against `query` in the fuzzy worker. An empty query needs no matching, so it
 * answers at once (the palette opens with these); while the worker works on a new query, see
 * `interimMatches`.
 */
export function useWorkerMatches(
  set: string,
  items: readonly Matchable[],
  query: string,
  matcher: FuzzyMatcher = sharedFuzzyMatcher(),
): ItemMatch[] {
  const [answer, setAnswer] = useState<Answer | null>(null);
  const empty = query.trim() === "";

  useEffect(() => {
    matcher.load(set, items);
  }, [matcher, set, items]);

  useEffect(() => {
    if (empty) return;
    let live = true;
    void matcher.match(set, query).then((matches) => {
      if (live) setAnswer({ query, items, matches });
    });
    return () => {
      live = false;
    };
  }, [matcher, set, items, query, empty]);

  const opening = matchItems("", items);
  if (empty) return opening;
  // Before the worker's first answer for these items (the first keystroke after opening), the
  // rows shown on opening stand in, matched again here: the rows don't empty while it starts.
  const latest = answer?.items === items ? answer : { query: "", items, matches: opening };
  return interimMatches(latest, query);
}
