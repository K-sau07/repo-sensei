"use client";

/**
 * The approval card (8.12), in its order: what the fix is and how ready it is to merge; the root
 * cause (one sentence, opening to the lines that show it); the reproducing test failing, then
 * passing; the plan (its steps edited in place: planned again); the diff in reading order once
 * written; the risk and what depends on the code; what couldn't be verified; and the actions.
 *
 * A person approves the **plan** (the patch is written and verified after): a member who can
 * write to the repository, the revision they read; a high-risk fix needs two of them.
 */
import {
  Check,
  ChevronRight,
  ExternalLink,
  Pencil,
  Plus,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useId, useState } from "react";

import { BlastRadius } from "@/components/changes/blast-radius";
import { RiskBadge } from "@/components/fixes/fix-badges";
import { useRepoSlug } from "@/components/repo/use-repo";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import type { Blast } from "@/lib/api/changes";
import { ApiError } from "@/lib/api/errors";
import {
  fixPlanOf,
  patchOf,
  reproOf,
  rootCauseOf,
  SOURCE_LABELS,
  verificationOf,
  type Fix,
  type FixPlan,
} from "@/lib/api/fixes";
import { codePath, repoPath } from "@/lib/routes";

import { DiffView } from "./diff-view";
import { useFixActions, useFixApprovals } from "./use-fixes";

export function ApprovalCard({ fix }: { fix: Fix }) {
  const plan = fixPlanOf(fix);
  const verification = verificationOf(fix);
  return (
    <section
      aria-label="Approval card"
      data-testid="approval-card"
      className="flex flex-col gap-5 rounded-xl border border-line p-5"
    >
      <header className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">{fix.title}</h2>
        <span className="text-xs text-fg-muted">{SOURCE_LABELS[fix.source]}</span>
        <span className="ml-auto text-sm" data-testid="merge-readiness">
          {verification?.readiness
            ? `Merge readiness ${String(verification.readiness.score)}/5: ${verification.readiness.reason}`
            : "Merge readiness: once the patch is verified"}
        </span>
      </header>
      <RootCauseChain fix={fix} />
      <ReproEvidence fix={fix} />
      {plan && <PlanSection fix={fix} plan={plan} />}
      <section aria-labelledby="diff-heading" className="flex flex-col gap-2">
        <h3 id="diff-heading" className="text-sm font-medium text-fg-muted">
          The change
        </h3>
        {patchOf(fix)?.diff ? (
          <DiffView diff={patchOf(fix)?.diff ?? ""} />
        ) : (
          <p className="text-xs text-fg-muted">Written and verified once the plan is approved.</p>
        )}
      </section>
      <RiskPanel fix={fix} plan={plan} />
      <section aria-labelledby="unverified-heading" className="flex flex-col gap-1">
        <h3 id="unverified-heading" className="text-sm font-medium text-fg-muted">
          What couldn&apos;t be verified
        </h3>
        {verification ? (
          verification.unverified.length > 0 ? (
            <ul className="flex list-disc flex-col gap-1 pl-5 text-xs" data-testid="unverified">
              {verification.unverified.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-fg-muted">Nothing left unverified.</p>
          )
        ) : (
          <p className="text-xs text-fg-muted">Listed once the patch is verified.</p>
        )}
      </section>
      {plan && <Actions fix={fix} plan={plan} />}
    </section>
  );
}

function RootCauseChain({ fix }: { fix: Fix }) {
  const { org, repo } = useRepoSlug();
  const cause = rootCauseOf(fix);
  if (!cause) return null;
  const at = (path: string, start: number, end: number) =>
    codePath(org, repo, path, {
      lines: start === end ? String(start) : `${String(start)}-${String(end)}`,
    });
  return (
    <section
      aria-labelledby="cause-heading"
      className="flex flex-col gap-1.5"
      data-testid="root-cause"
    >
      <h3 id="cause-heading" className="text-sm font-medium text-fg-muted">
        Root cause
      </h3>
      <p className="text-sm">
        {cause.claim}{" "}
        <Link
          href={at(cause.path, cause.start_line, cause.end_line)}
          className="font-mono text-xs hover:underline"
        >
          {cause.path}:{cause.start_line}
          {cause.end_line !== cause.start_line ? `–${String(cause.end_line)}` : ""}
        </Link>
      </p>
      {cause.seen_in_repro && (
        <p className="text-xs text-fg-muted">
          The reproducing test&apos;s failure runs through this file.
        </p>
      )}
      {cause.hypotheses.length > 0 && (
        <details className="group text-xs">
          <summary className="flex cursor-pointer items-center gap-1 text-fg-muted">
            <ChevronRight
              aria-hidden
              className="size-3 transition-transform group-open:rotate-90"
            />
            How it was found ({cause.hypotheses.length} hypothes
            {cause.hypotheses.length === 1 ? "is" : "es"})
          </summary>
          <ol className="mt-2 flex flex-col gap-2 pl-4">
            {cause.hypotheses.map((h) => (
              <li key={h.claim} className="flex flex-col gap-1">
                <span>
                  {h.claim}{" "}
                  <span className="text-fg-muted">({Math.round(h.likelihood * 100)}%)</span>
                </span>
                <span className="flex flex-wrap gap-1">
                  {h.evidence.map((e) => (
                    <Link
                      key={`${e.path}:${String(e.start_line)}`}
                      href={at(e.path, e.start_line, e.end_line)}
                      className="rounded-sm border border-line px-1.5 py-0.5 font-mono hover:bg-surface-2"
                    >
                      {e.path}:{e.start_line}
                    </Link>
                  ))}
                </span>
              </li>
            ))}
          </ol>
          {cause.suspect_commit && (
            <p className="mt-2 text-fg-muted">
              Introduced in <span className="font-mono">{cause.suspect_commit.slice(0, 12)}</span>
            </p>
          )}
        </details>
      )}
    </section>
  );
}

function ReproEvidence({ fix }: { fix: Fix }) {
  const repro = reproOf(fix);
  const verification = verificationOf(fix);
  if (!repro) return null;
  const failing = repro.runs[0]?.output ?? repro.tries.at(-1)?.output ?? "";
  const passing = verification?.repro_runs[0];
  return (
    <section
      aria-labelledby="repro-heading"
      className="flex flex-col gap-2"
      data-testid="repro-evidence"
    >
      <h3 id="repro-heading" className="text-sm font-medium text-fg-muted">
        Reproduced{repro.test_path ? ` by ${repro.test_path}` : ""}
      </h3>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <figure className="flex flex-col gap-1">
          <figcaption className="text-xs text-danger">Before the fix: it fails</figcaption>
          <pre className="max-h-48 overflow-auto rounded-md bg-surface-2 p-2 text-xs whitespace-pre-wrap">
            {failing || "No output kept."}
          </pre>
        </figure>
        <figure className="flex flex-col gap-1">
          <figcaption className={passing ? "text-xs text-ok" : "text-xs text-fg-muted"}>
            With the fix: {passing ? "it passes" : "once verified"}
          </figcaption>
          <pre className="max-h-48 overflow-auto rounded-md bg-surface-2 p-2 text-xs whitespace-pre-wrap">
            {passing?.output ?? "—"}
          </pre>
        </figure>
      </div>
    </section>
  );
}

type Step = FixPlan["steps"][number];

function PlanSection({ fix, plan }: { fix: Fix; plan: FixPlan }) {
  return (
    <section aria-labelledby="plan-heading" className="flex flex-col gap-2" data-testid="fix-plan">
      <h3 id="plan-heading" className="text-sm font-medium text-fg-muted">
        Plan (revision {plan.revision})
      </h3>
      <p className="text-sm">{plan.summary}</p>
      <ol className="flex list-decimal flex-col gap-1 pl-5 text-sm">
        {plan.steps.map((step, i) => (
          <li key={`${step.path}:${String(i)}`}>
            {step.description} <span className="font-mono text-xs text-fg-muted">{step.path}</span>
          </li>
        ))}
      </ol>
      {fix.status === "awaiting_approval" && plan.files.length > 0 && (
        <p className="text-xs text-fg-muted">
          Files:{" "}
          {plan.files.map((f) => `${f.path}${f.change === "create" ? " (new)" : ""}`).join(", ")}
        </p>
      )}
    </section>
  );
}

function RiskPanel({ fix, plan }: { fix: Fix; plan: FixPlan | null }) {
  const verification = verificationOf(fix);
  const risk = verification?.risk ?? plan?.risk ?? null;
  const cause = fix.root_cause as { blast_radius?: Blast | null } | null;
  return (
    <section
      aria-labelledby="risk-heading"
      className="flex flex-col gap-2"
      data-testid="risk-panel"
    >
      <h3 id="risk-heading" className="flex items-center gap-2 text-sm font-medium text-fg-muted">
        Risk {fix.risk && <RiskBadge risk={fix.risk} />}
      </h3>
      {risk && risk.reasons.length > 0 && (
        <p className="text-xs text-fg-muted">{risk.reasons.join("; ")}</p>
      )}
      <BlastRadius blast={cause?.blast_radius ?? null} />
    </section>
  );
}

function Actions({ fix, plan }: { fix: Fix; plan: FixPlan }) {
  const id = useId();
  const { org, repo } = useRepoSlug();
  const { role, me } = useOrgRole();
  const actions = useFixActions(fix.id);
  const approvals = useFixApprovals(fix.id);
  const [comment, setComment] = useState("");
  const [editing, setEditing] = useState(false);
  const [steps, setSteps] = useState<Step[]>(plan.steps);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const waiting = fix.status === "awaiting_approval";
  const member = roleAtLeast(role, "member");
  const mine = (approvals.data ?? []).some(
    (a) =>
      a.decision === "approve" && a.plan_revision === plan.revision && a.user_id === me.data?.id,
  );
  const approved = (approvals.data ?? []).filter(
    (a) => a.decision === "approve" && a.plan_revision === plan.revision,
  ).length;
  const said = comment.trim() || null;
  const act = async (go: () => Promise<unknown>) => {
    setError(null);
    try {
      await go();
      setConfirming(false);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "That didn't go through.");
    }
  };
  // ⌘↵ (Ctrl+Enter) asks to approve, as the button does.
  useEffect(() => {
    if (!waiting || !member || mine) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setConfirming(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [waiting, member, mine]);
  const timeline = fix.run_id ? repoPath(org, repo, `runs/${fix.run_id}`) : null;
  return (
    <section
      aria-label="Decide on the plan"
      className="flex flex-col gap-2 border-t border-line pt-4"
    >
      {waiting && member && editing && (
        <div className="flex flex-col gap-2" data-testid="plan-editor">
          {steps.map((step, i) => (
            <div key={i} className="flex items-center gap-2">
              <Input
                aria-label={`Step ${String(i + 1)}`}
                value={step.description}
                onChange={(event) => {
                  setSteps(
                    steps.map((s, j) => (j === i ? { ...s, description: event.target.value } : s)),
                  );
                }}
              />
              <span className="shrink-0 font-mono text-xs text-fg-muted">{step.path}</span>
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                aria-label={`Remove step ${String(i + 1)}`}
                onClick={() => {
                  setSteps(steps.filter((_, j) => j !== i));
                }}
              >
                <Trash2 aria-hidden />
              </Button>
            </div>
          ))}
          <div>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => {
                setSteps([
                  ...steps,
                  { description: "", path: plan.files[0]?.path ?? steps.at(-1)?.path ?? "" },
                ]);
              }}
            >
              <Plus aria-hidden />
              Add a step
            </Button>
          </div>
        </div>
      )}
      {waiting && member && (
        <>
          <label htmlFor={`${id}-comment`} className="text-xs font-medium">
            A note (optional; asking for changes needs one or an edited plan)
          </label>
          <Textarea
            id={`${id}-comment`}
            rows={2}
            value={comment}
            onChange={(event) => {
              setComment(event.target.value);
            }}
          />
        </>
      )}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {waiting && member && (
          <>
            <Button
              type="button"
              disabled={actions.busy || mine || editing}
              onClick={() => {
                setConfirming(true);
              }}
            >
              <Check aria-hidden />
              {mine ? "You approved it" : "Approve plan"}
              <kbd className="ml-1 text-[0.7rem] opacity-70">⌘↵</kbd>
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={actions.busy || (!editing && said === null)}
              onClick={() =>
                void act(() =>
                  actions.decide({
                    decision: "request_changes",
                    plan_revision: plan.revision,
                    comment: said,
                    ...(editing
                      ? { plan_edits: { steps: steps.filter((s) => s.description.trim()) } }
                      : {}),
                  }),
                )
              }
            >
              <RotateCcw aria-hidden />
              Request changes
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={actions.busy}
              onClick={() => {
                setEditing(!editing);
                setSteps(plan.steps);
              }}
            >
              <Pencil aria-hidden />
              {editing ? "Stop editing" : "Edit plan"}
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
          </>
        )}
        {timeline && (
          <Link
            href={timeline}
            className="ml-auto inline-flex items-center gap-1 text-xs hover:underline"
          >
            Open run timeline
            <ExternalLink aria-hidden className="size-3" />
          </Link>
        )}
      </div>
      {waiting && fix.approvals_needed === 2 && (
        <p className="text-xs text-fg-muted" data-testid="approvals-count">
          High risk: {approved} of 2 people approved (the one who asked can&apos;t be the only one).
        </p>
      )}
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Approve this plan?</AlertDialogTitle>
            <AlertDialogDescription>
              The patch is written to it and verified (the reproducing test, the whole suite,
              anti-gaming checks), then opened as a draft pull request. Nothing is merged.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
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
              Approve
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
