"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";

import { useMe } from "@/hooks/use-me";
import { recordHistory } from "@/lib/palette/history";
import { describeLocation } from "@/lib/palette/locations";

/** Remembers each page visited (in the signed-in user's history), for the palette's "Recent"
 * and its recency boost. */
export function PaletteHistoryRecorder() {
  const pathname = usePathname();
  const user = useMe().data?.id ?? null;
  useEffect(() => {
    const place = describeLocation(pathname);
    if (place) recordHistory(user, place);
  }, [pathname, user]);
  return null;
}
