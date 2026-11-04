"use client";

/** The Diagrams product page's example (10.3): a diagram on the canvas, read-only, with how
 * much of it the code verifies. */
import { ShieldCheck } from "lucide-react";
import { useState } from "react";

import { DiagramCanvas } from "@/components/diagrams/diagram-canvas";
import { verifiedPercent, type Spec, type SpecNode } from "@/lib/api/diagrams";

export function DiagramDemo({
  spec,
  mermaid,
  verifiedRatio,
}: {
  spec: Spec;
  mermaid: string;
  verifiedRatio: number;
}) {
  const [selected, setSelected] = useState<SpecNode | null>(null);
  return (
    <div className="flex flex-col gap-3">
      <span className="inline-flex w-fit items-center gap-1 rounded-full border border-ok/30 px-2 py-0.5 text-xs text-ok">
        <ShieldCheck aria-hidden className="size-3.5" />
        {verifiedPercent(verifiedRatio)} statically verified
      </span>
      <div className="h-[44rem] overflow-hidden rounded-xl border border-line">
        <DiagramCanvas
          spec={spec}
          edges={spec.edges}
          mermaid={mermaid}
          title={spec.title}
          selected={selected?.id ?? null}
          onSelect={setSelected}
          initialDirection="DOWN"
        />
      </div>
      <p className="min-h-5 text-sm text-fg-muted" aria-live="polite">
        {selected?.citation
          ? `${selected.label}: ${selected.citation.path}, lines ${String(selected.citation.start_line)}–${String(selected.citation.end_line)}`
          : "Choose a box to see the lines it stands for."}
      </p>
    </div>
  );
}
