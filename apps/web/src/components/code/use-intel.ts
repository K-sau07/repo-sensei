"use client";

/**
 * The code viewer's intelligence (3.14): the symbols around a line, a file's outline and a
 * symbol's neighbours in the graph, read as the files are (at the snapshot `useCodeLocation`
 * says); what a word under the cursor names; and the page's state (the symbol under the cursor,
 * the references drawer, the go-to-definition history).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { create } from "zustand";

import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import {
  CALL_TYPES,
  candidatesOf,
  NEIGHBORS_LIMIT,
  type EdgeType,
  type GraphHop,
  type GraphNeighbors,
  type GraphNode,
} from "@/lib/api/graph";
import type { SymbolAt, SymbolOutline, SymbolsAt } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";

import { useCodeLocation, type Snapshot } from "./use-code";

// A snapshot's symbols and graph never change, like its files.
const STALE_MS = 5 * 60_000;

const at = ({ ref }: Snapshot) => (ref ? { ref } : {});

export function symbolsAtOptions(snapshot: Snapshot, path: string, line: number) {
  const { org, repo, ref, version } = snapshot;
  return {
    queryKey: queryKeys.symbolsAt(org, repo, ref, version, path, line),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<SymbolsAt> => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/symbols/at", {
        params: { path: { org, repo }, query: { path, line, ...at(snapshot) } },
        signal,
      });
      if (!data) throw new Error("no symbols");
      return data;
    },
    staleTime: STALE_MS,
  };
}

export function outlineOptions(snapshot: Snapshot, path: string) {
  const { org, repo, ref, version } = snapshot;
  return {
    queryKey: queryKeys.symbolOutline(org, repo, ref, version, path),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<SymbolOutline> => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/symbols/outline", {
        params: { path: { org, repo }, query: { path, ...at(snapshot) } },
        signal,
      });
      if (!data) throw new Error("no outline");
      return data;
    },
    staleTime: STALE_MS,
  };
}

export interface NeighborQuery {
  direction: "out" | "in" | "both";
  /** None: every type. */
  types?: readonly EdgeType[];
  depth?: number;
  limit?: number;
}

export function neighborsOptions(snapshot: Snapshot, symbol: string, query: NeighborQuery) {
  const { org, repo, ref, version } = snapshot;
  const { direction, types, depth = 1, limit = NEIGHBORS_LIMIT } = query;
  const key = { direction, types: types?.join(",") ?? null, depth, limit };
  return {
    queryKey: queryKeys.graphNeighbors(org, repo, ref, version, symbol, key),
    queryFn: async ({ signal }: { signal: AbortSignal }): Promise<GraphNeighbors> => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/graph/neighbors", {
        params: {
          path: { org, repo },
          query: {
            symbol,
            direction,
            depth,
            limit,
            ...(key.types ? { types: key.types } : {}),
            ...at(snapshot),
          },
        },
        signal,
      });
      if (!data) throw new Error("no neighbours");
      return data;
    },
    staleTime: STALE_MS,
  };
}

/** The callers of a symbol (what reaches it by a call, a render, a hook or an injection). */
export const CALLERS: NeighborQuery = { direction: "in", types: CALL_TYPES };
export const CALLEES: NeighborQuery = { direction: "out", types: CALL_TYPES };

/** A symbol the viewer can show a card for, go to, or find the references of. */
export interface CodeSymbol {
  symbol_key: string;
  name: string;
  kind: string;
  /** Null: outside the repository (a library's). */
  path: string | null;
  start_line: number;
  end_line: number;
  /** The word is its own definition's name (no definition to go to: it's here). */
  definedHere: boolean;
}

/** A graph node as a symbol the actions take. */
export const symbolOf = (node: GraphNode): CodeSymbol => ({
  symbol_key: node.symbol_key,
  name: node.name,
  kind: node.kind,
  path: node.path,
  start_line: node.start_line,
  end_line: node.end_line,
  definedHere: false,
});

const fromDefinition = (symbol: SymbolAt, path: string): CodeSymbol => ({
  symbol_key: symbol.symbol_key,
  name: symbol.name,
  kind: symbol.kind,
  path,
  start_line: symbol.start_line,
  end_line: symbol.end_line,
  definedHere: true,
});

// A definition's name is on its first lines (after a decorator or two).
const HEADER_LINES = 3;

/** A symbol a word can name, and the edge it's known by (none: the word is its definition). */
export interface Candidate {
  symbol: CodeSymbol;
  hop: GraphHop | null;
}

/** What a word names (several: the reader picks one), or why that can't be known. */
export interface Resolution {
  candidates: Candidate[];
  /** Why nothing could be looked up (the graph not built yet), for the reader. */
  unavailable: string | null;
}

const NOT_BUILT = new Set(["graph_not_ready", "not_indexed", "repo_not_indexed"]);

/**
 * What `word` on `line` of `path` names: a definition's own name, or what the innermost
 * definition around it that uses something by that name uses (its calls, references, imports,
 * bases); none when the graph has nothing by that name there (a local, a keyword).
 */
export function useResolve() {
  const location = useCodeLocation();
  const client = useQueryClient();
  const { org, repo, ref, version } = location;
  return useCallback(
    async (path: string, line: number, word: string): Promise<Resolution> => {
      const snapshot = { org, repo, ref, version };
      try {
        const around = (await client.query(symbolsAtOptions(snapshot, path, line))).symbols;
        const own = around.find(
          (s) => s.name === word && line >= s.start_line && line - s.start_line < HEADER_LINES,
        );
        if (own)
          return {
            candidates: [{ symbol: fromDefinition(own, path), hop: null }],
            unavailable: null,
          };
        for (const symbol of around) {
          const out = await client.query(
            neighborsOptions(snapshot, symbol.symbol_key, { direction: "out" }),
          );
          const hops = candidatesOf(out.hops, word, line, path);
          if (hops.length > 0) {
            return {
              candidates: hops.map((hop) => ({ symbol: symbolOf(hop.node), hop })),
              unavailable: null,
            };
          }
        }
        return { candidates: [], unavailable: null };
      } catch (error) {
        const code = error instanceof ApiError ? error.code : null;
        let unavailable = "Couldn't look the definition up. Try again.";
        if (code && NOT_BUILT.has(code)) {
          unavailable =
            "The code graph isn't built for this commit yet, so definitions can't be found.";
        } else if (code === "rate_limited") {
          unavailable = "Too many lookups in a minute. Try again in a moment.";
        }
        return { candidates: [], unavailable };
      }
    },
    [client, org, repo, ref, version],
  );
}

export function useOutline(path: string, enabled = true) {
  const location = useCodeLocation();
  return useQuery({ ...outlineOptions(location, path), enabled: enabled && location.ready });
}

export function useNeighbors(symbol: string | null, query: NeighborQuery) {
  const location = useCodeLocation();
  return useQuery({
    ...neighborsOptions(location, symbol ?? "", query),
    enabled: symbol !== null && location.ready,
  });
}

/** A symbol's signature and docstring: the definitions at its first line, by its key. */
export function useSymbolDetails(symbol: CodeSymbol | null) {
  const location = useCodeLocation();
  const path = symbol?.path ?? "";
  const line = Math.max(symbol?.start_line ?? 1, 1);
  return useQuery({
    ...symbolsAtOptions(location, path, line),
    enabled: symbol?.path != null && location.ready,
    select: (data: SymbolsAt) =>
      data.symbols.find((s) => s.symbol_key === symbol?.symbol_key) ?? null,
  });
}

/** A place in the code, to go back to. */
export interface CodePlace {
  path: string;
  line: number;
}

/** Where the cursor is in the open file, and the word it's on. */
export interface Cursor {
  path: string;
  line: number;
  word: string | null;
}

interface IntelState {
  cursor: Cursor | null;
  /** The symbol the rail's graph was centred on, and the definition the cursor was in then: once
   * the cursor is in another, the graph follows the cursor again. */
  centre: { symbol: CodeSymbol; at: string | null } | null;
  /** The symbol whose references the drawer lists (null: closed). */
  references: CodeSymbol | null;
  /** Where each go-to-definition left from, the latest last. */
  back: CodePlace[];
  setCursor: (cursor: Cursor | null) => void;
  setCentre: (centre: { symbol: CodeSymbol; at: string | null } | null) => void;
  setReferences: (symbol: CodeSymbol | null) => void;
  pushBack: (place: CodePlace) => void;
  popBack: () => CodePlace | null;
}

const BACK_MAX = 50;

export const useIntelStore = create<IntelState>()((set, get) => ({
  cursor: null,
  centre: null,
  references: null,
  back: [],
  setCursor: (cursor) => {
    set({ cursor });
  },
  setCentre: (centre) => {
    set({ centre });
  },
  setReferences: (references) => {
    set({ references });
  },
  pushBack: (place) => {
    set({ back: [...get().back, place].slice(-BACK_MAX) });
  },
  popBack: () => {
    const { back } = get();
    const place = back.at(-1) ?? null;
    if (place) set({ back: back.slice(0, -1) });
    return place;
  },
}));
