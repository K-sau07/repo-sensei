"use client";

/**
 * The code of the hop picked, docked under the trace: the file at the lines that prove the hop
 * (the call site; the entry's own lines), lit and scrolled to, in the code browser's viewer. Links
 * open the place, and the hop's definition, in the code browser.
 */
import { Code2, CornerDownRight, FileX2, X } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useState } from "react";

import { useCodeLocation, useFile } from "@/components/code/use-code";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/errors";
import {
  definitionPlace,
  hopPlace,
  placeLabel,
  type HopPlace,
  type TraceHop,
} from "@/lib/api/trace";
import { formatLines, type LineRange } from "@/lib/code/lines";
import { codePath } from "@/lib/routes";

import { kindLabel, TRACE_LANES } from "./meta";

// CodeMirror runs in the browser only, in its own chunk (the code browser's).
const CodeViewer = dynamic(
  () => import("@/components/code/code-viewer").then((module) => module.CodeViewer),
  { ssr: false, loading: () => <CodeSkeleton /> },
);

function CodeSkeleton() {
  return (
    <div role="status" aria-label="Loading the code" className="flex flex-col gap-2 p-4">
      {Array.from({ length: 6 }, (_, index) => (
        <Skeleton
          key={index}
          className="h-3.5"
          style={{ width: `${String(30 + ((index * 29) % 60))}%` }}
        />
      ))}
    </div>
  );
}

function Viewer({ place }: { place: HopPlace }) {
  const file = useFile(place.path);
  const at = { start: place.start, end: place.end };
  // A range picked in the gutter stays while this place is shown (another place is a new viewer).
  const [picked, setPicked] = useState<LineRange | null>(null);
  const lines = picked ?? at;
  const error = file.error instanceof ApiError ? file.error : null;
  if (file.data) {
    return (
      <CodeViewer
        path={place.path}
        language={file.data.language}
        content={file.data.content}
        lines={lines}
        onLinesChange={setPicked}
        wrap={false}
      />
    );
  }
  if (error?.status === 404) {
    return (
      <p className="flex items-center gap-2 p-4 text-sm text-fg-muted">
        <FileX2 aria-hidden className="size-4" />
        {place.path} isn&apos;t in this snapshot of the repository.
      </p>
    );
  }
  if (file.isError) {
    return (
      <div className="flex items-center gap-2 p-4 text-sm text-fg-muted">
        Couldn&apos;t load the code.
        <Button variant="outline" size="xs" onClick={() => void file.refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  return <CodeSkeleton />;
}

export function HopCode({
  hop,
  how,
  onClose,
}: {
  hop: TraceHop;
  /** How the hop is reached (see `howReached`): "starts here", "entry", or the edge's verb. */
  how: string;
  onClose: () => void;
}) {
  const { org, repo, ref } = useCodeLocation();
  const place = hopPlace(hop);
  const definition = definitionPlace(hop.node);
  const lane = TRACE_LANES[hop.layer];
  const open = (at: HopPlace) =>
    codePath(org, repo, at.path, { ref, lines: formatLines({ start: at.start, end: at.end }) });
  return (
    <section
      aria-label={`Code of ${hop.node.name}`}
      data-testid="hop-code"
      className="flex h-80 shrink-0 flex-col overflow-hidden rounded-[10px] border border-line bg-surface-1"
    >
      <header className="flex h-9 shrink-0 items-center gap-2 border-b border-line px-3 text-sm">
        <span aria-hidden className={lane.text}>
          {lane.glyph}
        </span>
        <h2 className="truncate font-mono font-medium">{hop.node.name}</h2>
        <span className="shrink-0 text-xs text-fg-muted">
          {kindLabel(hop.node.kind)}
          {hop.edge ? `, ${how} at` : `, ${how === "entry" ? "the entry" : "where it starts"}, at`}
        </span>
        {place && (
          <span className="min-w-0 truncate font-mono text-xs text-fg-faint">
            {placeLabel(place)}
          </span>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-1">
          {place && (
            <Link
              href={open(place)}
              className="inline-flex h-6 items-center gap-1 rounded-sm px-2 text-xs text-fg-muted hover:bg-surface-2 hover:text-fg"
            >
              <Code2 aria-hidden className="size-3.5" />
              Open in code
            </Link>
          )}
          {definition && definition.path !== place?.path && (
            <Link
              href={open(definition)}
              className="inline-flex h-6 items-center gap-1 rounded-sm px-2 text-xs text-fg-muted hover:bg-surface-2 hover:text-fg"
            >
              <CornerDownRight aria-hidden className="size-3.5" />
              Definition
            </Link>
          )}
          <Button variant="ghost" size="icon-sm" aria-label="Close the code" onClick={onClose}>
            <X aria-hidden />
          </Button>
        </div>
      </header>
      <div className="flex min-h-0 flex-1 flex-col">
        {place ? (
          <Viewer key={`${place.path}:${String(place.start)}`} place={place} />
        ) : (
          <p className="p-4 text-sm text-fg-muted">This hop is in no file of the repository.</p>
        )}
      </div>
    </section>
  );
}
