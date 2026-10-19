"use client";

import { RouteError } from "@/components/feedback/route-error";

/** Errors inside an org page keep the shell (sidebar, ⌘K) usable around them. */
export default function OrgError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return <RouteError error={error} reset={reset} title="This page couldn't load" />;
}
