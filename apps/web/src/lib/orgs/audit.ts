/**
 * The org's audit log (10.7.2), as its viewer says it: each action in words, who did it, and
 * what it was done to (from the event's target and details).
 */
import type { Schemas } from "@/lib/api/types";

export type AuditAction = Schemas["AuditAction"];
export type AuditEvent = Schemas["AuditEventOut"];

export const ACTION_LABELS: Record<AuditAction, string> = {
  "auth.login": "Signed in",
  "auth.logout": "Signed out",
  "auth.logout_all": "Signed out everywhere",
  "auth.account_delete": "Deleted their account",
  "org.create": "Created the organization",
  "org.update": "Renamed the organization",
  "org.delete": "Deleted the organization",
  "org.restore": "Restored the organization",
  "org.purge": "Purged the organization",
  "invitation.create": "Invited someone",
  "invitation.revoke": "Revoked an invitation",
  "invitation.accept": "Accepted an invitation",
  "member.role_change": "Changed a member's role",
  "member.remove": "Removed a member",
  "member.leave": "Left the organization",
  "github.install.linked": "Linked a GitHub installation",
  "github.install.deleted": "GitHub installation removed",
  "github.install.suspended": "GitHub installation suspended",
  "github.install.unsuspended": "GitHub installation resumed",
  "github.install.permissions_accepted": "GitHub permissions accepted",
  "repo.connected": "Connected a repository",
  "repo.settings_update": "Changed a repository's settings",
  "repo.disconnected": "Disconnected a repository",
  "repo.published": "Published a repository",
  "repo.unpublished": "Unpublished a repository",
  "repo.index_started": "Started an index run",
  "repo.index_cancelled": "Cancelled an index run",
  "agent.cost_cap_warning": "Neared the day's agent budget",
  "conversation.shared": "Shared a conversation",
  "conversation.unshared": "Stopped sharing a conversation",
  "diagram.shared": "Shared a diagram",
  "diagram.unshared": "Stopped sharing a diagram",
  "api_key.create": "Created an API key",
  "api_key.revoke": "Revoked an API key",
  "oauth.authorize": "Authorized an app",
  "oauth.refresh_reused": "A reused token was refused",
  "fix.create": "Started a fix",
  "fix.decision": "Decided on a plan",
  "sentry.connect": "Connected Sentry",
  "sentry.disconnect": "Disconnected Sentry",
  "slack.connect": "Connected Slack",
  "slack.disconnect": "Disconnected Slack",
};

export const AUDIT_ACTIONS = Object.keys(ACTION_LABELS) as AuditAction[];

/** An action this page has no words for yet (a newer API's) is shown as it is written. */
export function actionLabel(action: string): string {
  return (ACTION_LABELS as Record<string, string>)[action] ?? action;
}

/** Who did it: their login, or what acted (the system, an API key, an app). */
export function actorLabel(event: AuditEvent): string {
  if (event.actor_login) return `@${event.actor_login}`;
  if (event.actor_type === "system") return "repo-sensei";
  if (event.actor_type === "api_key") return "an API key";
  if (event.actor_type === "oauth") return "an app";
  return "a deleted account";
}

const DETAIL_KEYS = ["full_name", "email", "slug", "name", "role", "to", "kind", "channel"];

/** What it was done to, from its details: a repository's name, an invitee's email, whose
 * role went from what to what (``logins``: members' logins by id, for a member's target). */
export function targetLabel(
  event: AuditEvent,
  logins: ReadonlyMap<string, string> = new Map(),
): string | null {
  const meta = event.meta;
  const member =
    event.target_type === "user" && event.target_id ? logins.get(event.target_id) : undefined;
  if (event.action === "member.role_change" && typeof meta.from === "string") {
    const change = `${meta.from} → ${typeof meta.to === "string" ? meta.to : "?"}`;
    return member ? `@${member}: ${change}` : change;
  }
  if (member) return `@${member}`;
  for (const key of DETAIL_KEYS) {
    const value = meta[key];
    if (typeof value === "string" && value) return value;
  }
  return null;
}
