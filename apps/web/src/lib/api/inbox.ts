/**
 * The inbox (8.10, 8.12): what someone was told in an org, and how each kind reads. Kinds fall in
 * three groups, as the page filters them: approvals waiting, things that finished (a pull
 * request opened, a repository indexed) and failures (a fix or index run that stopped, the org's
 * spend near its cap).
 */
import type { Schemas } from "./types";

export type InboxItem = Schemas["InboxItemOut"];
export type InboxPage = Schemas["InboxPage"];
export type NotificationKind = Schemas["NotificationKind"];
export type KindPreference = Schemas["KindPreference"];
export type Preferences = Schemas["PreferencesOut"];

export const GROUPS = ["approvals", "finished", "failures"] as const;
export type InboxGroup = (typeof GROUPS)[number];

export const GROUP_LABELS: Record<InboxGroup, string> = {
  approvals: "Approvals",
  finished: "Finished",
  failures: "Failures",
};

export const KIND_GROUP: Record<NotificationKind, InboxGroup> = {
  approval_needed: "approvals",
  pr_opened: "finished",
  index_done: "finished",
  fix_failed: "failures",
  index_failed: "failures",
  cost_cap: "failures",
};

/** What a kind is called where one item is shown (a screen reader's word for its icon). */
export const KIND_LABELS: Record<NotificationKind, string> = {
  approval_needed: "Approval needed",
  pr_opened: "Pull request opened",
  index_done: "Indexed",
  fix_failed: "Stopped",
  index_failed: "Indexing failed",
  cost_cap: "Spend near the cap",
};

/** The kinds in ``group`` (null: every kind), as the API filters them. */
export function kindsOf(group: InboxGroup | null): NotificationKind[] | null {
  if (group === null) return null;
  return (Object.keys(KIND_GROUP) as NotificationKind[]).filter(
    (kind) => KIND_GROUP[kind] === group,
  );
}

/** An item that waits on someone's approval, and says where (the `a` key opens it). */
export function isApproval(item: InboxItem): boolean {
  return item.kind === "approval_needed" && item.link !== null;
}

/** A link into the app, or none: only paths are followed, never another site (``//host``, or
 * ``/\host``, which browsers read as ``//host``; control characters are dropped by them too). */
export function appPath(link: string | null | undefined): string | null {
  if (!link?.startsWith("/") || link.startsWith("//")) return null;
  return /[\\\u0000-\u001f\u007f]/.test(link) ? null : link;
}

export function inboxHref(item: InboxItem): string | null {
  return appPath(item.link);
}
