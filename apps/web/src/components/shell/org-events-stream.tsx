"use client";

import { useInboxEvents } from "@/components/inbox/use-inbox";
import { useOrgEvents } from "@/components/repos/use-org-events";

import { useOrgSlug } from "./use-org-slug";

/** One org event stream per tab, for live status dots everywhere in the shell, and one of the
 * person's inbox (its badge, and a word when something arrives). */
export function OrgEventsStream() {
  const org = useOrgSlug();
  useOrgEvents(org);
  useInboxEvents(org);
  return null;
}
