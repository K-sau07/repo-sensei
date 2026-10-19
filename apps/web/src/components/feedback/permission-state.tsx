import { cn } from "cn";
import { LockKeyhole } from "lucide-react";
import type { ReactNode } from "react";

interface PermissionStateProps {
  /** The role this needs, e.g. "admin". */
  requiredRole: string;
  yourRole?: string;
  /** Who can change it, e.g. "Org owners and admins". */
  grantedBy?: string;
  /** Usually a "Request access" button. */
  action?: ReactNode;
  className?: string;
}

export function PermissionState({
  requiredRole,
  yourRole,
  grantedBy = "An org owner or admin",
  action,
  className,
}: PermissionStateProps) {
  return (
    <section
      aria-label="Permission needed"
      className={cn(
        "flex flex-col items-center gap-3 rounded-xl border border-line p-10 text-center",
        className,
      )}
    >
      <LockKeyhole aria-hidden className="size-8 text-fg-faint" />
      <h2 className="text-base font-semibold">You need the {requiredRole} role</h2>
      <p className="max-w-md text-sm text-fg-muted">
        {yourRole ? `You're a ${yourRole} in this organization. ` : ""}
        {grantedBy} can change your role.
      </p>
      {action && <div className="mt-2">{action}</div>}
    </section>
  );
}
