import { useSyncExternalStore } from "react";

import { detectPlatform, type Platform } from "@/lib/shortcuts";

const noop = () => () => undefined;

/** "apple" or "other". Server rendering assumes "other"; the client corrects it after hydration. */
export function usePlatform(): Platform {
  return useSyncExternalStore(
    noop,
    () => detectPlatform(navigator),
    () => "other",
  );
}
