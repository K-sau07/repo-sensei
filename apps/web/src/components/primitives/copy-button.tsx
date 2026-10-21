"use client";

import { Check, Copy } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

interface CopyButtonProps {
  value: string;
  /** Accessible name, e.g. "Copy request ID". */
  label?: string;
  /** What the button shows until it confirms (a copy icon by default). */
  icon?: ReactNode;
  className?: string;
}

const RESET_AFTER_MS = 2000;

type CopyState = { status: "idle" } | { status: "copied" | "failed"; at: number };

async function writeClipboard(value: string): Promise<void> {
  // navigator.clipboard is missing outside secure contexts (plain http on a LAN address).
  if (typeof navigator === "undefined" || !("clipboard" in navigator)) {
    throw new Error("Clipboard unavailable");
  }
  await navigator.clipboard.writeText(value);
}

/** Copies `value` to the clipboard and confirms for two seconds (announced politely). */
export function CopyButton({ value, label = "Copy", icon, className }: CopyButtonProps) {
  const [state, setState] = useState<CopyState>({ status: "idle" });

  // Keyed on `at`, so copying again restarts the two seconds.
  useEffect(() => {
    if (state.status === "idle") return;
    const timer = setTimeout(() => {
      setState({ status: "idle" });
    }, RESET_AFTER_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [state]);

  const announcement =
    state.status === "copied" ? "Copied" : state.status === "failed" ? "Copy failed" : "";

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={label}
        className={className}
        onClick={() => {
          writeClipboard(value).then(
            () => {
              setState({ status: "copied", at: Date.now() });
            },
            () => {
              setState({ status: "failed", at: Date.now() });
            },
          );
        }}
      >
        {state.status === "copied" ? <Check className="text-ok" /> : (icon ?? <Copy />)}
      </Button>
      {/* The key re-mounts the region so a repeat copy is announced again. */}
      <span key={state.status === "idle" ? "idle" : state.at} role="status" className="sr-only">
        {announcement}
      </span>
    </>
  );
}
