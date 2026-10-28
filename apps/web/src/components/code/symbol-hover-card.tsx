"use client";

/**
 * A symbol's card, on hover in the code viewer (rendered into CodeMirror's tooltip): its kind,
 * name and layer, signature and docstring, how many callers it has, and what to do with it.
 */
import {
  CornerDownRight,
  ListTree,
  MessageSquareText,
  Radar,
  Route as RouteIcon,
} from "lucide-react";
import type { ReactNode } from "react";

import { KeyHint } from "@/components/primitives/key-hint";
import { LayerBadge } from "@/components/primitives/layer-badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { badgeLayer, countLabel } from "@/lib/api/graph";
import { shortcut, type ShortcutId } from "@/lib/shortcut-registry";

import { CALLERS, useNeighbors, useSymbolDetails, type CodeSymbol } from "./use-intel";

/** The card's actions, which the page carries out. */
export interface SymbolActions {
  goToDefinition: (symbol: CodeSymbol) => void;
  showReferences: (symbol: CodeSymbol) => void;
  trace: (symbol: CodeSymbol) => void;
}

function Action({
  label,
  icon,
  keys,
  onClick,
  soon,
}: {
  label: string;
  icon: ReactNode;
  keys?: ShortcutId;
  onClick?: () => void;
  /** Built in a later phase: shown, disabled, with when. */
  soon?: string;
}) {
  const content = (
    <>
      {icon}
      {label}
      {keys && <KeyHint shortcut={shortcut(keys).keys} />}
    </>
  );
  if (!soon) {
    return (
      <Button
        variant="ghost"
        size="xs"
        onClick={onClick}
        aria-keyshortcuts={keys ? shortcut(keys).keys : undefined}
      >
        {content}
      </Button>
    );
  }
  return (
    <Tooltip>
      {/* Not `disabled`, which takes it out of the tab order and the pointer's reach: the
       * reason has to be readable. */}
      <TooltipTrigger
        render={<Button variant="ghost" size="xs" aria-disabled="true" className="opacity-50" />}
      >
        {content}
      </TooltipTrigger>
      <TooltipContent>{soon}</TooltipContent>
    </Tooltip>
  );
}

export function SymbolHoverCard({
  symbol,
  actions,
}: {
  symbol: CodeSymbol;
  actions: SymbolActions;
}) {
  const details = useSymbolDetails(symbol);
  const callers = useNeighbors(symbol.symbol_key, CALLERS);
  const lane = callers.data ? badgeLayer(callers.data.root.lane) : null;
  const signature = details.data?.signature ?? null;
  const docstring = details.data?.docstring ?? null;
  return (
    <section
      aria-label={`${symbol.kind} ${symbol.name}`}
      data-testid="symbol-card"
      className="flex w-[min(28rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-[10px] border border-line-strong bg-surface-2 text-sm shadow-(--elevation-2)"
    >
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <span className="text-xs text-fg-muted">{symbol.kind.replace("_", " ")}</span>
        <span className="min-w-0 flex-1 truncate font-mono font-medium">{symbol.name}</span>
        {lane && <LayerBadge layer={lane} />}
      </header>
      <div className="flex flex-col gap-2 px-3 py-2">
        {signature && (
          <pre className="max-h-24 overflow-hidden font-mono text-xs whitespace-pre-wrap text-fg">
            {signature}
          </pre>
        )}
        {docstring && (
          <p className="line-clamp-5 text-xs whitespace-pre-line text-fg-muted">{docstring}</p>
        )}
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-muted">
          <span className="truncate font-mono">
            {symbol.path ? `${symbol.path}:${String(symbol.start_line)}` : "Outside the repository"}
          </span>
          {callers.data && (
            <span className="tabular-nums" data-testid="callers-count">
              {countLabel(callers.data.hops.length, callers.data.truncated)}{" "}
              {callers.data.hops.length === 1 && !callers.data.truncated ? "caller" : "callers"}
            </span>
          )}
          {symbol.definedHere && <span>Defined here</span>}
        </p>
      </div>
      <footer className="flex flex-wrap items-center gap-1 border-t border-line px-2 py-1.5">
        {!symbol.definedHere && symbol.path && (
          <Action
            label="Go to definition"
            icon={<CornerDownRight aria-hidden />}
            keys="code-definition"
            onClick={() => {
              actions.goToDefinition(symbol);
            }}
          />
        )}
        <Action
          label="References"
          icon={<ListTree aria-hidden />}
          keys="code-references"
          onClick={() => {
            actions.showReferences(symbol);
          }}
        />
        <Action
          label="Trace"
          icon={<RouteIcon aria-hidden />}
          keys="trace"
          onClick={() => {
            actions.trace(symbol);
          }}
        />
        <Action label="Impact" icon={<Radar aria-hidden />} soon="Impact arrives in phase 6" />
        <Action
          label="Ask about"
          icon={<MessageSquareText aria-hidden />}
          soon="Ask arrives in phase 4"
        />
      </footer>
    </section>
  );
}
