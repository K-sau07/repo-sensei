import { cn } from "cn";

interface LiveDotProps {
  /** Announced to screen readers, e.g. "Indexing" or "Agent running". */
  label: string;
  className?: string;
}

/** The breathing signal dot for live work. Static when the user prefers reduced motion. */
export function LiveDot({ label, className }: LiveDotProps) {
  return (
    <span className={cn("relative inline-flex size-2", className)}>
      <span
        aria-hidden
        className="absolute inset-0 rounded-full bg-signal opacity-60 motion-safe:animate-[live-breathe_2.4s_var(--ease-inout)_infinite]"
      />
      <span aria-hidden className="relative size-2 rounded-full bg-signal" />
      <span className="sr-only">{label}</span>
    </span>
  );
}
