/**
 * The graph explorer's state in its URL: the tab, the level, the node in focus and how many hops
 * around it, a search, a path's start and end, the table instead of the canvas, a cycle to show,
 * and whether calls guessed by their names alone are counted. Defaults stay out of the URL.
 */
import type { Route } from "next";
import {
  createSerializer,
  parseAsBoolean,
  parseAsInteger,
  parseAsString,
  parseAsStringLiteral,
  useQueryStates,
  type inferParserType,
} from "nuqs";

import { DEP_LEVELS } from "@/lib/api/deps";
import { repoPath } from "@/lib/routes";

export const GRAPH_TABS = ["graph", "dsm", "cycles", "packages"] as const;
export type GraphTab = (typeof GRAPH_TABS)[number];

export const MAX_DEGREE = 3;

export const graphParsers = {
  tab: parseAsStringLiteral(GRAPH_TABS).withDefault("graph"),
  level: parseAsStringLiteral(DEP_LEVELS).withDefault("module"),
  node: parseAsString,
  degree: parseAsInteger.withDefault(1),
  q: parseAsString.withDefault(""),
  from: parseAsString,
  to: parseAsString,
  table: parseAsBoolean.withDefault(false),
  cycle: parseAsInteger,
  guesses: parseAsBoolean.withDefault(false),
};

export type GraphState = inferParserType<typeof graphParsers>;

export function useGraphState() {
  return useQueryStates(graphParsers, { history: "replace", scroll: false });
}

const serialize = createSerializer(graphParsers);

/** The graph page with this state. */
export function graphHref(org: string, repo: string, state: Partial<GraphState> = {}): Route {
  return serialize(repoPath(org, repo, "graph"), state) as Route;
}
