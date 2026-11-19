/**
 * Where a deployment doesn't run fixes (no sandbox host for them: ``fixes_enabled`` false on
 * /me), the Fix and Change pages say so instead of offering a run no one would pick up.
 */
import { Wrench } from "lucide-react";

import { EmptyState } from "@/components/feedback/empty-state";

export function FixesOff({ what }: { what: "fix" | "change" }) {
  return (
    <EmptyState
      icon={<Wrench aria-hidden />}
      title={
        what === "fix"
          ? "Fix isn't available on this deployment"
          : "Changes aren't available on this deployment"
      }
      description="They run a repository's own tests in a sandbox this server doesn't have. Everything else works here: Ask, search, trace, diagrams, the graph and impact."
    />
  );
}
