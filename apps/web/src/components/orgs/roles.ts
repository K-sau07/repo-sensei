import type { Role } from "@/lib/api/types";

export const ROLE_LABELS: Record<Role, string> = {
  viewer: "Viewer",
  member: "Member",
  admin: "Admin",
  owner: "Owner",
};

export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  viewer: "Asks and browses; can't change anything.",
  member: "Asks, traces and requests fixes and changes.",
  admin: "Also manages repositories, members and settings.",
  owner: "Everything, including deleting the organization.",
};

/** Roles you may give others: only owners can make owners (same rule as the API). */
export function assignableRoles(myRole: Role): Role[] {
  return myRole === "owner"
    ? ["viewer", "member", "admin", "owner"]
    : ["viewer", "member", "admin"];
}
