"use client";

import { cn } from "cn";
import { OctagonX, RotateCw } from "lucide-react";
import type { ReactNode } from "react";

import { CopyButton } from "@/components/primitives/copy-button";
import { Button } from "@/components/ui/button";

interface ErrorStateProps {
  /** What failed, in the user's terms: "Couldn't load members". */
  title: string;
  description?: ReactNode;
  onRetry?: () => void;
  /** The X-Request-Id of the failed call, so support can find it in the logs. */
  requestId?: string;
  className?: string;
}

export function ErrorState({ title, description, onRetry, requestId, className }: ErrorStateProps) {
  return (
    <section
      role="alert"
      className={cn(
        "flex flex-col items-start gap-3 rounded-xl border border-danger/30 bg-danger/6 p-6",
        className,
      )}
    >
      <div className="flex items-center gap-2 text-danger">
        <OctagonX aria-hidden className="size-4" />
        <h2 className="text-sm font-semibold">{title}</h2>
      </div>
      {description && <p className="text-sm text-fg-muted">{description}</p>}
      <div className="flex flex-wrap items-center gap-3">
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RotateCw aria-hidden data-icon="inline-start" />
            Try again
          </Button>
        )}
        {requestId && (
          <span className="inline-flex items-center gap-1 text-xs text-fg-muted">
            Request ID <code className="font-mono">{requestId}</code>
            <CopyButton value={requestId} label="Copy request ID" />
          </span>
        )}
      </div>
    </section>
  );
}
