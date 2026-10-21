"use client";

import { useEffect } from "react";

import { ApiError } from "@/lib/api/errors";

import { ErrorState } from "./error-state";

interface RouteErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
  title?: string;
}

/** Shared body of the error boundaries: what failed, a retry, and a reference for support. */
export function RouteError({ error, reset, title = "Something went wrong" }: RouteErrorProps) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  // API failures carry the request id; server render failures carry Next's digest.
  const reference = error instanceof ApiError ? error.requestId : error.digest;
  return (
    <div className="mx-auto w-full max-w-xl p-8">
      <ErrorState
        title={title}
        description={
          error instanceof ApiError
            ? error.message
            : "The page hit an unexpected error. Trying again usually fixes it."
        }
        onRetry={reset}
        {...(reference ? { requestId: reference } : {})}
      />
    </div>
  );
}
