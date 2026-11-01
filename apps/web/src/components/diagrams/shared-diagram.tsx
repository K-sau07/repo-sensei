"use client";

/**
 * `/d/[token]` (6.6): a shared diagram, read-only, signed in or not. A viewer who can't read the
 * repository sees its steps and how sure each edge is, but no code (no files, lines or links).
 */
import { ShieldCheck } from "lucide-react";
import { useParams } from "next/navigation";
import { useState } from "react";

import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { $api } from "@/lib/api/client";
import { outline, specOf, verifiedPercent, type SpecNode } from "@/lib/api/diagrams";
import { ApiError } from "@/lib/api/errors";

import { DiagramCanvas } from "./diagram-canvas";

export function SharedDiagramView() {
  const { token } = useParams<{ token: string }>();
  const [selected, setSelected] = useState<SpecNode | null>(null);
  const shared = $api.useQuery(
    "get",
    "/api/v1/d/{token}",
    { params: { path: { token } } },
    { retry: false },
  );
  if (shared.isPending) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <DelayedSpinner active label="Loading the diagram" />
      </div>
    );
  }
  if (shared.isError) {
    const gone = shared.error instanceof ApiError && shared.error.status === 404;
    return (
      <div className="mx-auto flex max-w-md flex-1 flex-col justify-center gap-2 p-6 text-center">
        <h1 className="text-lg font-semibold">
          {gone ? "This link doesn't open anything" : "This diagram couldn't be loaded"}
        </h1>
        <p className="text-sm text-fg-muted">
          {gone
            ? "It may have been reset or stopped by whoever shared it."
            : "Try again in a moment."}
        </p>
      </div>
    );
  }
  const { diagram, repo, redacted } = shared.data;
  const spec = specOf(diagram);
  return (
    <div className="flex min-h-dvh flex-col" data-testid="shared-diagram">
      <header className="flex flex-wrap items-center gap-3 border-b border-line px-6 py-3">
        <div className="flex flex-col">
          <p className="text-xs text-fg-muted">
            Shared from repo-sensei · {repo ?? "a private repository"}
          </p>
          <h1 className="text-lg font-semibold">{diagram.title}</h1>
        </div>
        <span className="inline-flex items-center gap-1 rounded-full border border-ok/30 px-2 py-0.5 text-xs text-ok">
          <ShieldCheck aria-hidden className="size-3.5" />
          {verifiedPercent(diagram.verified_ratio)} statically verified
        </span>
        {redacted ? (
          <span className="text-xs text-fg-muted">
            The code is hidden: you can&apos;t read the repository.
          </span>
        ) : null}
      </header>
      <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[minmax(0,1fr)_22rem]">
        <DiagramCanvas
          spec={spec}
          edges={spec.edges}
          mermaid={diagram.mermaid}
          title={diagram.title}
          selected={selected?.id ?? null}
          onSelect={setSelected}
        />
        <section aria-label="Outline" className="hidden border-l border-line md:block">
          <ol className="list-decimal py-2 pr-3 pl-8 text-xs">
            {outline(spec).map((step, i) => (
              <li key={`${String(i)}-${step}`} className="py-0.5">
                {step}
              </li>
            ))}
          </ol>
        </section>
      </div>
    </div>
  );
}
