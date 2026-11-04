"use client";

/**
 * The Trace product page's example (10.3): a real trace in the swimlanes, read-only. Choosing a
 * hop says where it is in the code; its beam pulses once, as when a trace resolves.
 */
import { useState } from "react";

import { TraceSwimlane } from "@/components/trace/trace-swimlane";
import type { TracePath } from "@/lib/api/trace";

export function TraceDemo({ path }: { path: TracePath }) {
  const [selected, setSelected] = useState<number | null>(null);
  const hop = selected === null ? null : path.hops[selected];
  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto rounded-xl border border-line bg-surface-1 p-4">
        <TraceSwimlane path={path} selected={selected} onSelect={setSelected} pulse />
      </div>
      <p className="min-h-5 text-sm text-fg-muted" aria-live="polite">
        {hop?.node.path
          ? `${hop.node.name}: ${hop.node.path}${hop.node.lines ? `, lines ${String(hop.node.lines[0])}–${String(hop.node.lines[1])}` : ""}`
          : "Choose a step to see where it is in the code; scroll sideways to follow it."}
      </p>
    </div>
  );
}
