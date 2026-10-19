import { cn } from "cn";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export type Provenance = "precise" | "heuristic" | "inferred";

const PROVENANCE: Record<Provenance, { label: string; description: string; className: string }> = {
  precise: {
    label: "Precise",
    description: "Resolved from the code itself (types, imports or routes).",
    className: "border-ok/40 text-ok",
  },
  heuristic: {
    label: "Heuristic",
    description: "Matched by naming and structure rules; usually right, not proven.",
    className: "border-dashed border-line-strong text-fg-muted",
  },
  inferred: {
    label: "Inferred",
    description: "Suggested by the model from surrounding code; check before relying on it.",
    className: "border-dotted border-warn/60 text-warn",
  },
};

interface ProvenanceTagProps {
  provenance: Provenance;
  className?: string;
}

/** How an edge or fact was established. The line style differs too, not only the color. */
export function ProvenanceTag({ provenance, className }: ProvenanceTagProps) {
  const { label, description, className: tone } = PROVENANCE[provenance];
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            type="button"
            className={cn(
              "inline-flex h-5 items-center rounded-sm border px-1.5 text-xs font-medium",
              tone,
              className,
            )}
          />
        }
      >
        {label}
      </TooltipTrigger>
      <TooltipContent>{description}</TooltipContent>
    </Tooltip>
  );
}
