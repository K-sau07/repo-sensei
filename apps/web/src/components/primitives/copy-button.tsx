"use client";

import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";

interface CopyButtonProps {
  value: string;
  /** Accessible name, e.g. "Copy request ID". */
  label?: string;
  className?: string;
}

const RESET_AFTER_MS = 2000;

/** Copies `value` to the clipboard and confirms for two seconds (announced politely). */
export function CopyButton({ value, label = "Copy", className }: CopyButtonProps) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    if (state === "idle") return;
    const timer = setTimeout(() => {
      setState("idle");
    }, RESET_AFTER_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [state]);

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={label}
        className={className}
        onClick={() => {
          navigator.clipboard.writeText(value).then(
            () => {
              setState("copied");
            },
            () => {
              setState("failed");
            },
          );
        }}
      >
        {state === "copied" ? <Check className="text-ok" /> : <Copy />}
      </Button>
      <span role="status" className="sr-only">
        {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : ""}
      </span>
    </>
  );
}
