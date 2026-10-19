"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";

import { onAuthExpired } from "@/lib/api/auth-events";
import { ApiError } from "@/lib/api/errors";

const Devtools =
  process.env.NODE_ENV === "development"
    ? lazy(() =>
        import("@tanstack/react-query-devtools").then((module) => ({
          default: module.ReactQueryDevtools,
        })),
      )
    : () => null;

const MAX_RETRIES = 2;

/** Retry server and network failures, never client errors (4xx): those won't fix themselves. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.isClientError) return false;
  return failureCount < MAX_RETRIES;
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { staleTime: 30_000, retry: shouldRetry },
      mutations: { retry: false },
    },
  });
}

/** Where to send someone whose session ended, keeping where they were. */
export function loginUrlFor(location: Pick<Location, "pathname" | "search">): string | null {
  if (location.pathname.startsWith("/login")) return null;
  const next = `${location.pathname}${location.search}`;
  return `/login?next=${encodeURIComponent(next)}`;
}

export function QueryProvider({ children }: { children: ReactNode }) {
  const [queryClient] = useState(makeQueryClient);

  useEffect(
    () =>
      onAuthExpired(() => {
        const target = loginUrlFor(window.location);
        if (target) window.location.assign(target);
      }),
    [],
  );

  return (
    <QueryClientProvider client={queryClient}>
      {children}
      <Suspense>
        <Devtools buttonPosition="bottom-left" />
      </Suspense>
    </QueryClientProvider>
  );
}
