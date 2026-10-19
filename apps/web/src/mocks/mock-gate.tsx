"use client";

import { useEffect, useState, type ReactNode } from "react";

/**
 * With NEXT_PUBLIC_MOCKS=1 the API is served by MSW in the browser, so the UI runs without the
 * backend. Children render only once the worker is intercepting, so no request slips through.
 *
 * The env check is written inline at each use: Next replaces it with a literal at build time, so
 * without the flag the bundler drops this code and MSW is not built into the app at all.
 */
export function MockGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(process.env.NEXT_PUBLIC_MOCKS !== "1");

  useEffect(() => {
    if (process.env.NEXT_PUBLIC_MOCKS === "1") {
      let cancelled = false;
      void import("./browser")
        .then(({ worker }) => worker.start({ onUnhandledFrame: "bypass", quiet: true }))
        .catch((error: unknown) => {
          // Don't leave a blank page: carry on against the real API and say why.
          console.error("Mock API failed to start; using the real API.", error);
        })
        .finally(() => {
          if (!cancelled) setReady(true);
        });
      return () => {
        cancelled = true;
      };
    }
  }, []);

  return ready ? children : null;
}
