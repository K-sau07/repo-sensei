/**
 * The trace view's state in its URL: `?from=` (the entry: a node's key, a route, a file or a
 * name), `path` (the path tab, from 1), `hop` (the hop whose code is open, from 0) and `view`
 * ("list" for the ordered list instead of the swimlanes); and the overview's `?trace=` and `path`,
 * the trace path its constellation draws. Defaults stay out of the URL.
 */
import type { Route } from "next";
import {
  createSerializer,
  parseAsInteger,
  parseAsString,
  parseAsStringLiteral,
  type inferParserType,
} from "nuqs";

import { repoPath } from "@/lib/routes";

export const TRACE_VIEWS = ["lanes", "list"] as const;
export type TraceView = (typeof TRACE_VIEWS)[number];

export const traceParsers = {
  from: parseAsString,
  path: parseAsInteger.withDefault(1),
  hop: parseAsInteger,
  view: parseAsStringLiteral(TRACE_VIEWS).withDefault("lanes"),
};

export type TraceState = inferParserType<typeof traceParsers>;

const serializeTrace = createSerializer(traceParsers);

/** The trace page with this state (no `from`: the entry picker). */
export function traceHref(org: string, repo: string, state: Partial<TraceState> = {}): Route {
  return serializeTrace(repoPath(org, repo, "trace"), state) as Route;
}

export const overviewTraceParsers = {
  trace: parseAsString,
  path: parseAsInteger.withDefault(1),
};

const serializeOverview = createSerializer(overviewTraceParsers);

/** The repository's overview, its constellation drawing the trace from `entry` (its `path`). */
export function overviewTraceHref(org: string, repo: string, entry: string, path = 1): Route {
  return serializeOverview(repoPath(org, repo), { trace: entry, path }) as Route;
}

/** A path tab from the URL, within the paths there are (1 if it's out of range). */
export function pathIndex(path: number, count: number): number {
  return Number.isInteger(path) && path >= 1 && path <= count ? path - 1 : 0;
}
