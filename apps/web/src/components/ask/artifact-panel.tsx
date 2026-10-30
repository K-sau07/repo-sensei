"use client";

/**
 * The artifact panel (4.11.3): cards promoted from answers, kept open beside the thread. A
 * card promoted again (the same symbol's neighbours, the same trace, asked later) is a new
 * version of it when what it shows changed, so an answer's view can be compared with a later
 * one's. Kept for the page's life, not stored.
 */
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import type { Route } from "next";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { cardKey } from "@/lib/api/cards";

import { artifactTitle, CardBody, type Artifact, type CodeHref } from "./tool-cards";

interface Pinned {
  key: string;
  versions: Artifact[];
}

function keyOf(artifact: Artifact): string {
  return artifact.kind === "card"
    ? cardKey(artifact.card)
    : `endpoints:${artifact.rows.map((r) => r.node.symbol_key).join(",")}`;
}

export function useArtifacts() {
  const [pinned, setPinned] = useState<Pinned[]>([]);
  const [shown, setShown] = useState<{ key: string; version: number } | null>(null);
  const promote = (artifact: Artifact) => {
    const key = keyOf(artifact);
    const found = pinned.find((p) => p.key === key);
    const last = found?.versions.at(-1);
    const same = last !== undefined && JSON.stringify(last) === JSON.stringify(artifact);
    const versions = found ? (same ? found.versions : [...found.versions, artifact]) : [artifact];
    setPinned([...pinned.filter((p) => p.key !== key), { key, versions }]);
    setShown({ key, version: versions.length - 1 });
  };
  const current = shown ? pinned.find((p) => p.key === shown.key) : undefined;
  return {
    pinned,
    current: current && shown ? { pinned: current, version: shown.version } : null,
    promote,
    show: (key: string, version: number) => {
      setShown({ key, version });
    },
    close: () => {
      setShown(null);
    },
  };
}

export function ArtifactPanel({
  pinned,
  current,
  onShow,
  onClose,
  codeHref,
  traceHref,
}: {
  pinned: readonly Pinned[];
  current: { pinned: Pinned; version: number };
  onShow: (key: string, version: number) => void;
  onClose: () => void;
  codeHref?: CodeHref | undefined;
  traceHref?: ((entry: string) => Route | null) | undefined;
}) {
  const { versions, key } = current.pinned;
  const artifact = versions[current.version];
  if (!artifact) return null;
  const title = artifactTitle(artifact);
  return (
    <aside
      aria-label="Artifact"
      className="flex h-full min-h-0 flex-col border-l border-border"
      data-testid="artifact-panel"
    >
      <header className="flex items-center gap-1 border-b border-border px-3 py-2">
        <h2 className="min-w-0 flex-1 truncate text-sm font-medium">{title}</h2>
        {versions.length > 1 ? (
          <div className="flex items-center gap-0.5 text-xs text-fg-muted">
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Earlier version"
              disabled={current.version === 0}
              onClick={() => {
                onShow(key, current.version - 1);
              }}
            >
              <ChevronLeft aria-hidden />
            </Button>
            <span data-testid="artifact-version">
              v{current.version + 1} of {versions.length}
            </span>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Later version"
              disabled={current.version === versions.length - 1}
              onClick={() => {
                onShow(key, current.version + 1);
              }}
            >
              <ChevronRight aria-hidden />
            </Button>
          </div>
        ) : null}
        <Button variant="ghost" size="icon-sm" aria-label="Close the artifact" onClick={onClose}>
          <X aria-hidden />
        </Button>
      </header>
      <div className="flex-1 overflow-auto p-3">
        <CardBody artifact={artifact} codeHref={codeHref} traceHref={traceHref} />
      </div>
      {pinned.length > 1 ? (
        <nav aria-label="Promoted" className="border-t border-border p-2">
          <ul className="flex flex-wrap gap-1">
            {pinned.map((p) => {
              const last = p.versions.at(-1);
              if (!last) return null;
              return (
                <li key={p.key}>
                  <Button
                    variant={p.key === key ? "secondary" : "ghost"}
                    size="sm"
                    className="h-6 text-xs"
                    aria-current={p.key === key ? "true" : undefined}
                    onClick={() => {
                      onShow(p.key, p.versions.length - 1);
                    }}
                  >
                    {artifactTitle(last)}
                  </Button>
                </li>
              );
            })}
          </ul>
        </nav>
      ) : null}
    </aside>
  );
}
