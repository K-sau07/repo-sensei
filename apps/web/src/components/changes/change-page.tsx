"use client";

/**
 * `/[org]/[repo]/changes/[changeId]` (9.3): one requested change. What was asked, its stages
 * (followed live), and its plan canvas: while the plan waits for approval, a member who can
 * write to the repository edits it (planned again with the edits), approves it as it is, or
 * rejects it; a high-risk change needs two approvers. Once approved: what was built (its
 * files and tests) and how it was verified.
 */
import { ArrowLeft, Check, RotateCcw, X } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useId, useState } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { RiskBadge, StatusBadge } from "@/components/fixes/fix-badges";
import { RunStages } from "@/components/fixes/run-stages";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { editsOf, planOf, type Change, type ChangePlan } from "@/lib/api/changes";
import { ApiError } from "@/lib/api/errors";
import { isOpen } from "@/lib/api/fixes";
import { repoPath } from "@/lib/routes";

import { draftOf, PlanCanvas, type Draft } from "./plan-canvas";
import { useChange, useChangeActions } from "./use-changes";

export function ChangePage() {
  const { org, repo } = useRepoSlug();
  const { changeId } = useParams<{ changeId: string }>();
  const change = useChange(changeId);
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-6" data-testid="change-page">
      <Link
        href={repoPath(org, repo, "changes")}
        className="inline-flex items-center gap-1 text-xs text-fg-muted hover:underline"
      >
        <ArrowLeft aria-hidden className="size-3" />
        Changes
      </Link>
      {change.isPending ? (
        <Skeleton className="h-48" />
      ) : change.isError ? (
        <ErrorState title="Couldn't load the change" onRetry={() => void change.refetch()} />
      ) : (
        <Loaded change={change.data} />
      )}
    </div>
  );
}

function Loaded({ change }: { change: Change }) {
  const plan = planOf(change);
  const planning = !plan && isOpen(change.status);
  return (
    <>
      <header className="flex flex-col gap-2">
        <h1 className="text-lg font-semibold">{change.title}</h1>
        <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
          <StatusBadge status={change.status} />
          {change.risk && <RiskBadge risk={change.risk} />}
          <span>{change.author ? `@${change.author}` : "—"}</span>
          <RelativeTime date={change.created_at} className="text-fg-faint" />
          {change.pr_url && (
            <a href={change.pr_url} target="_blank" rel="noreferrer" className="hover:underline">
              Draft pull request<span className="sr-only"> (opens GitHub)</span>
            </a>
          )}
        </div>
        <RunStages kind="change" stage={change.stage} status={change.status} />
      </header>
      <section aria-labelledby="request-heading" className="flex flex-col gap-1">
        <h2 id="request-heading" className="text-sm font-medium text-fg-muted">
          What was asked
        </h2>
        <p className="text-sm whitespace-pre-wrap" data-testid="request">
          {change.ticket.text || change.title}
        </p>
      </section>
      {change.stop_reason && (
        <p className="rounded-md border border-line p-3 text-sm" data-testid="stop-reason">
          {change.stop_reason}
        </p>
      )}
      {planning && (
        <div role="status" className="flex flex-col gap-2" data-testid="planning">
          <p className="text-sm text-fg-muted">
            Planning the change: reading the code it touches and the patterns it should follow…
          </p>
          <Skeleton className="h-32" />
        </div>
      )}
      {/* A new revision is a new draft: the canvas starts again from it. */}
      {plan && <Plan key={plan.revision} change={change} plan={plan} />}
      <Built change={change} />
    </>
  );
}

function Plan({ change, plan }: { change: Change; plan: ChangePlan }) {
  const id = useId();
  const { role } = useOrgRole();
  const actions = useChangeActions(change.id);
  const [draft, setDraft] = useState<Draft>(() => draftOf(plan));
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);
  const waiting = change.status === "awaiting_approval";
  const editable = waiting && roleAtLeast(role, "member");
  const edits = editsOf(plan, draft);
  const act = async (go: () => Promise<unknown>) => {
    setError(null);
    try {
      await go();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "That didn't go through.");
    }
  };
  const said = comment.trim() || null;
  return (
    <>
      <PlanCanvas plan={plan} draft={draft} onDraft={setDraft} editable={editable} />
      {plan.requests.length > 0 && (
        <p className="text-xs text-fg-muted" data-testid="plan-requests">
          Planned again after {plan.requests.length} request
          {plan.requests.length === 1 ? "" : "s"} for changes, the last from @
          {plan.requests.at(-1)?.by}.
        </p>
      )}
      {editable && (
        <section
          aria-label="Decide on the plan"
          className="flex flex-col gap-2 rounded-md border border-line p-3"
        >
          <label htmlFor={`${id}-comment`} className="text-xs font-medium">
            A note for the planner (optional)
          </label>
          <Textarea
            id={`${id}-comment`}
            rows={2}
            value={comment}
            onChange={(event) => {
              setComment(event.target.value);
            }}
          />
          {error && (
            <p role="alert" className="text-sm text-danger">
              {error}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              disabled={actions.busy || edits !== null}
              onClick={() =>
                void act(() =>
                  actions.decide({
                    decision: "approve",
                    plan_revision: plan.revision,
                    comment: said,
                  }),
                )
              }
            >
              <Check aria-hidden />
              Approve plan
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={actions.busy || (edits === null && said === null)}
              onClick={() =>
                void act(() =>
                  edits
                    ? actions.edit({ plan_revision: plan.revision, edits, comment: said })
                    : actions.decide({
                        decision: "request_changes",
                        plan_revision: plan.revision,
                        comment: said,
                      }),
                )
              }
            >
              <RotateCcw aria-hidden />
              Plan again with {edits ? "my edits" : "my note"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={actions.busy}
              onClick={() =>
                void act(() =>
                  actions.decide({
                    decision: "reject",
                    plan_revision: plan.revision,
                    comment: said,
                  }),
                )
              }
            >
              <X aria-hidden />
              Reject
            </Button>
            {edits !== null && (
              <span className="text-xs text-fg-muted" data-testid="edits-pending">
                Edited: plan it again to approve the edited plan.
              </span>
            )}
            {change.approvals_needed === 2 && (
              <span className="text-xs text-fg-muted">High risk: two people approve it.</span>
            )}
          </div>
        </section>
      )}
    </>
  );
}

/** What was built and how it was verified, once there is something to show. */
function Built({ change }: { change: Change }) {
  const patch = change.patch as { files?: string[]; tests?: string[]; summary?: string } | null;
  const verification = change.verification as {
    readiness?: { score: number; reason: string };
    unverified?: string[];
  } | null;
  if (!patch && !verification) return null;
  return (
    <section aria-labelledby="built-heading" className="flex flex-col gap-2" data-testid="built">
      <h2 id="built-heading" className="text-sm font-medium text-fg-muted">
        What was built
      </h2>
      {patch?.summary && <p className="text-sm">{patch.summary}</p>}
      {patch && (
        <p className="text-xs text-fg-muted">
          {patch.files?.length ?? 0} file(s) changed, {patch.tests?.length ?? 0} new test file(s).
        </p>
      )}
      {verification?.readiness && (
        <p className="text-sm" data-testid="readiness">
          Merge readiness {verification.readiness.score}/5: {verification.readiness.reason}
        </p>
      )}
      {verification?.unverified && verification.unverified.length > 0 && (
        <>
          <h3 className="text-xs font-medium text-fg-muted">What couldn&apos;t be verified</h3>
          <ul className="flex list-disc flex-col gap-1 pl-5 text-xs">
            {verification.unverified.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
