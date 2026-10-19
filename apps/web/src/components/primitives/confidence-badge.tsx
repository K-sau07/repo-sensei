import { cn } from "cn";
import { CircleCheck, CircleDashed, TriangleAlert } from "lucide-react";

export type Confidence = "high" | "medium" | "low";

const CONFIDENCE = {
  high: { label: "High confidence", Icon: CircleCheck, className: "text-ok bg-ok/12" },
  medium: { label: "Medium confidence", Icon: CircleDashed, className: "text-warn bg-warn/12" },
  low: { label: "Low confidence", Icon: TriangleAlert, className: "text-danger bg-danger/12" },
} as const;

interface ConfidenceBadgeProps {
  confidence: Confidence;
  className?: string;
}

/** Answer or link confidence, shown with an icon and words as well as a color. */
export function ConfidenceBadge({ confidence, className }: ConfidenceBadgeProps) {
  const { label, Icon, className: tone } = CONFIDENCE[confidence];
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center gap-1 rounded-sm px-1.5 text-xs font-medium",
        tone,
        className,
      )}
    >
      <Icon aria-hidden className="size-3.5" />
      {label}
    </span>
  );
}
