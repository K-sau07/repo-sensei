"use client";

import { useOrgEvents } from "@/components/repos/use-org-events";

import { useOrgSlug } from "./use-org-slug";

/** One org event stream per tab, for live status dots everywhere in the shell. */
export function OrgEventsStream() {
  useOrgEvents(useOrgSlug());
  return null;
}
