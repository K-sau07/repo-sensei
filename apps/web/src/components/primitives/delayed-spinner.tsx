"use client";

import { cn } from "cn";
import { Loader2 } from "lucide-react";

import { useDelayedFlag } from "@/hooks/use-delayed-flag";

interface DelayedSpinnerProps {
  /** True while the work runs; the spinner shows after 200ms and stays at least 400ms. */
  active: boolean;
  label?: string;
  className?: string;
}

export function DelayedSpinner({ active, label = "Loading", className }: DelayedSpinnerProps) {
  const visible = useDelayedFlag(active);
  if (!visible) return null;
  return (
    <span role="status" className={cn("inline-flex items-center", className)}>
      <Loader2 aria-hidden className="size-4 animate-spin text-fg-muted" />
      <span className="sr-only">{label}</span>
    </span>
  );
}
