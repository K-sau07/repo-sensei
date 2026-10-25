"use client";

import { Clock } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import type { ApiError } from "@/lib/api/errors";

/** 429: when to try again, counted down; Retry waits for it. */
export function RateLimited({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  const [counted, setCounted] = useState(error);
  const [left, setLeft] = useState(error.retryAfter ?? 0);
  // Each 429 counts down from its own Retry-After (a retry can get another).
  if (counted !== error) {
    setCounted(error);
    setLeft(error.retryAfter ?? 0);
  }
  useEffect(() => {
    if (left <= 0) return;
    const timer = setTimeout(() => {
      setLeft((value) => value - 1);
    }, 1000);
    return () => {
      clearTimeout(timer);
    };
  }, [left]);
  return (
    <section
      role="alert"
      className="flex flex-col items-start gap-3 rounded-xl border border-warn/30 bg-warn/8 p-6"
    >
      <div className="flex items-center gap-2 text-warn">
        <Clock aria-hidden className="size-4" />
        <h3 className="text-sm font-semibold">Too many searches for now</h3>
      </div>
      <p className="text-sm text-fg-muted">
        {left > 0 ? (
          <>
            You can search again in <span className="tabular-nums">{left}</span>{" "}
            {left === 1 ? "second" : "seconds"}.
          </>
        ) : (
          "You can search again now."
        )}
      </p>
      <Button variant="outline" size="sm" disabled={left > 0} onClick={onRetry}>
        Try again
      </Button>
    </section>
  );
}
