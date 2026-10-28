"use client";

/**
 * The code viewer's intelligence, put together (3.14): what its hover cards, ⌘-click and the
 * keyboard do (`g d` go to definition, `g r` references, `t` trace, `⌥←` back), shared by the
 * viewer, the references drawer and the Symbol rail.
 */
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { toast } from "sonner";

import { useShortcut } from "@/hooks/use-shortcut";
import { innermostAt } from "@/lib/api/graph";
import type { SymbolAt } from "@/lib/api/types";
import { codeViewKey, crossToCode } from "@/lib/code/transition";
import { codePath, repoPath } from "@/lib/routes";

import type { ViewerIntel } from "./code-viewer";
import { DefinitionPicker, type DefinitionChoice } from "./definition-picker";
import { SymbolHoverCard, type SymbolActions } from "./symbol-hover-card";
import { useCodeLocation } from "./use-code";
import {
  useIntelStore,
  useOutline,
  useResolve,
  type CodePlace,
  type CodeSymbol,
} from "./use-intel";

// One toast at a time for go to definition: a second try replaces the first.
const TOAST_ID = "go-to-definition";

/** A definition of the outline as a symbol the actions take (it's defined where it is). */
export function definitionSymbol(symbol: SymbolAt, path: string): CodeSymbol {
  return {
    symbol_key: symbol.symbol_key,
    name: symbol.name,
    kind: symbol.kind,
    path,
    start_line: symbol.start_line,
    end_line: symbol.end_line,
    definedHere: true,
  };
}

/** The trace page, from a symbol. */
export function tracePath(org: string, repo: string, symbol: string): string {
  return `${repoPath(org, repo, "trace")}?from=${encodeURIComponent(symbol)}`;
}

/** Going somewhere in the code, and what the cards and lists do with a symbol. */
export function useCodeActions(): SymbolActions & { open: (place: CodePlace) => void } {
  const { org, repo, ref, path } = useCodeLocation();
  const router = useRouter();
  const setReferences = useIntelStore((state) => state.setReferences);
  const pushBack = useIntelStore((state) => state.pushBack);
  return useMemo(() => {
    const open = (place: CodePlace) => {
      const lines = { start: place.line, end: place.line };
      crossToCode(
        () => {
          router.push(codePath(org, repo, place.path, { ref, lines: String(place.line) }));
        },
        codeViewKey(place.path, lines) ?? place.path,
      );
    };
    return {
      open,
      goToDefinition: (symbol) => {
        if (!symbol.path) {
          toast(`${symbol.name} is defined outside the repository.`);
          return;
        }
        // Back comes here: the cursor's line in this file, or its top.
        const cursor = useIntelStore.getState().cursor;
        if (path) pushBack({ path, line: cursor?.path === path ? cursor.line : 1 });
        open({ path: symbol.path, line: Math.max(symbol.start_line, 1) });
      },
      showReferences: (symbol) => {
        setReferences(symbol);
      },
      trace: (symbol) => {
        router.push(tracePath(org, repo, symbol.symbol_key) as Parameters<typeof router.push>[0]);
      },
    };
  }, [org, repo, ref, path, router, setReferences, pushBack]);
}

/**
 * The viewer's side of it for the file at `path`: what it asks (`intel`), and what to render:
 * the hover card (a portal into CodeMirror's tooltip) and the picker for a name that could go
 * to several definitions. Binds the keyboard while the file is open.
 */
export function useCodeIntel(
  path: string,
  enabled: boolean,
): { intel: ViewerIntel; card: ReactNode; picker: ReactNode } {
  const resolveAll = useResolve();
  const actions = useCodeActions();
  const setCursor = useIntelStore((state) => state.setCursor);
  const popBack = useIntelStore((state) => state.popBack);
  const canGoBack = useIntelStore((state) => state.back.length > 0);
  const outline = useOutline(path, enabled);
  const [card, setCard] = useState<{ dom: HTMLElement; symbol: CodeSymbol } | null>(null);
  const [choice, setChoice] = useState<DefinitionChoice | null>(null);

  /** Go to what `word` names: its one definition, a pick of several, or why there's none. */
  const goTo = useCallback(
    (line: number, word: string, fromKeyboard: boolean) => {
      void resolveAll(path, line, word).then(({ candidates, unavailable }) => {
        const [only] = candidates;
        if (unavailable) toast(unavailable, { id: TOAST_ID });
        else if (!only) toast(`No definition found for ${word}.`, { id: TOAST_ID });
        else if (candidates.length > 1) setChoice({ word, candidates });
        else if (!only.symbol.definedHere) actions.goToDefinition(only.symbol);
        else if (fromKeyboard) toast(`This is where ${word} is defined.`, { id: TOAST_ID });
      });
    },
    [path, resolveAll, actions],
  );

  const intel = useMemo<ViewerIntel>(
    () => ({
      resolve: async (line, word) =>
        (await resolveAll(path, line, word)).candidates[0]?.symbol ?? null,
      onCardOpen: (dom, symbol) => {
        setCard({ dom, symbol });
      },
      onCardClose: (dom) => {
        setCard((open) => (open?.dom === dom ? null : open));
      },
      onGoTo: (line, word) => {
        goTo(line, word, false);
      },
      onCursor: (line, word) => {
        setCursor({ path, line, word });
      },
    }),
    [path, resolveAll, goTo, setCursor],
  );

  /** What the cursor is on: the symbol its word names, else the definition around it. */
  const atCursor = async (): Promise<CodeSymbol | null> => {
    const cursor = useIntelStore.getState().cursor;
    if (cursor?.path !== path) return null;
    const named = cursor.word
      ? (await resolveAll(path, cursor.line, cursor.word)).candidates[0]?.symbol
      : null;
    if (named) return named;
    const around = innermostAt(outline.data?.symbols ?? [], cursor.line);
    return around ? definitionSymbol(around, path) : null;
  };

  useShortcut(
    "code-definition",
    () => {
      const cursor = useIntelStore.getState().cursor;
      if (!cursor?.word || cursor.path !== path) {
        toast("Put the cursor on a name to go to its definition.", { id: TOAST_ID });
        return;
      }
      goTo(cursor.line, cursor.word, true);
    },
    { enabled, inEditor: true },
  );
  useShortcut(
    "code-references",
    () => {
      void atCursor().then((symbol) => {
        if (symbol) actions.showReferences(symbol);
        else toast("Put the cursor in a definition to find its references.");
      });
    },
    { enabled, inEditor: true },
  );
  useShortcut(
    "trace",
    () => {
      void atCursor().then((symbol) => {
        if (symbol) actions.trace(symbol);
        else toast("Put the cursor in a definition to trace it.");
      });
    },
    { enabled, inEditor: true },
  );
  useShortcut(
    "code-back",
    () => {
      const place = popBack();
      if (place) actions.open(place);
    },
    { enabled: enabled && canGoBack, inEditor: true },
  );

  return {
    intel,
    card: card
      ? createPortal(<SymbolHoverCard symbol={card.symbol} actions={actions} />, card.dom)
      : null,
    picker: (
      <DefinitionPicker
        choice={choice}
        onPick={(symbol) => {
          setChoice(null);
          actions.goToDefinition(symbol);
        }}
        onClose={() => {
          setChoice(null);
        }}
      />
    ),
  };
}
