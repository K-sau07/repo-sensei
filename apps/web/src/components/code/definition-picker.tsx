"use client";

/**
 * When a name could go to more than one definition (`g d`, ⌘-click): the candidates, surest
 * first, to pick one. One outside the repository (a library's) is listed but can't be opened.
 */
import { LayerBadge } from "@/components/primitives/layer-badge";
import { ProvenanceTag } from "@/components/primitives/provenance-tag";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { badgeLayer, provenanceTag } from "@/lib/api/graph";

import type { Candidate, CodeSymbol } from "./use-intel";

export interface DefinitionChoice {
  word: string;
  candidates: Candidate[];
}

export function DefinitionPicker({
  choice,
  onPick,
  onClose,
}: {
  choice: DefinitionChoice | null;
  onPick: (symbol: CodeSymbol) => void;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={choice !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            Where <code className="font-mono">{choice?.word}</code> goes
          </DialogTitle>
          <DialogDescription>
            The graph has {choice?.candidates.length ?? 0} definitions it could mean. Pick the one
            to open.
          </DialogDescription>
        </DialogHeader>
        <ul className="flex flex-col gap-1">
          {choice?.candidates.map(({ symbol, hop }) => {
            const lane = hop ? badgeLayer(hop.node.lane) : null;
            return (
              <li key={symbol.symbol_key}>
                <button
                  type="button"
                  disabled={symbol.path === null}
                  onClick={() => {
                    onPick(symbol);
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-mono text-sm text-fg">
                      {hop?.node.qualified_name ?? symbol.name}
                    </span>
                    <span className="block truncate text-xs text-fg-muted">
                      {symbol.path
                        ? `${symbol.path}:${String(symbol.start_line)}`
                        : "Outside the repository"}
                    </span>
                  </span>
                  {lane && <LayerBadge layer={lane} compact />}
                  {hop && (
                    <>
                      <ProvenanceTag provenance={provenanceTag(hop.edge.provenance)} />
                      <span className="w-9 text-right text-xs text-fg-muted tabular-nums">
                        {Math.round(hop.edge.confidence * 100)}%
                      </span>
                    </>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
