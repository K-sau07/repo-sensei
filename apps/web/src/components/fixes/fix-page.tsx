"use client";

/**
 * `/[org]/[repo]/fixes/[fixId]` (8.12): one fix, live. Its stages; what its agents are doing
 * (their steps, as they are recorded); a question about it, asked in Ask; and how it stands:
 *
 * - waiting for approval, or past it: the approval card (:mod:`./approval-card`);
 * - **not reproduced**: the attempts (each test run, and why it didn't count), no approve, and
 *   "retry with more context";
 * - **flaky**: the run matrix (which of the three runs failed for the reported reason);
 * - **blocked**: the anti-gaming rule it hit, and where;
 * - **failed**: why, and its run timeline.
 */
import { ArrowLeft, CircleHelp, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useId, useState, type SubmitEvent } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { LiveDot } from "@/components/primitives/live-dot";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { ApiError } from "@/lib/api/errors";
import {
  fixPlanOf,
  isOpen,
  patchOf,
  reproOf,
  rootCauseOf,
  RULE_LABELS,
  SOURCE_LABELS,
  verificationOf,
  type Finding,
  type Fix,
  prLink,
} from "@/lib/api/fixes";
import { repoPath } from "@/lib/routes";

import { ApprovalCard } from "./approval-card";
import { RiskBadge, StatusBadge } from "./fix-badges";
import { RunStages } from "./run-stages";
import { TracePreview } from "./trace-preview";
import { useFix, useFixActions, useFixActivity } from "./use-fixes";

export function FixPage() {
  const { org, repo } = useRepoSlug();
  const { fixId } = useParams<{ fixId: string }>();
  const fix = useFix(fixId);
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-5 p-6" data-testid="fix-page">
      <Link
        href={repoPath(org, repo, "fixes")}
        className="inline-flex items-center gap-1 text-xs text-fg-muted hover:underline"
      >
        <ArrowLeft aria-hidden className="size-3" />
        Fixes
      </Link>
      {fix.isPending ? (
        <Skeleton className="h-40" />
      ) : fix.isError ? (
        <ErrorState title="Couldn't load the fix" onRetry={() => void fix.refetch()} />
      ) : (
        <Loaded fix={fix.data} />
      )}
    </div>
  );
}

function Loaded({ fix }: { fix: Fix }) {
  const open = isOpen(fix.status);
  const activity = useFixActivity(fix.id, open && fix.status !== "awaiting_approval");
  const plan = fixPlanOf(fix);
  return (
    <>
      <header className="flex flex-col gap-2">
        <h1 className="text-lg font-semibold">{fix.title}</h1>
        <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
          <StatusBadge status={fix.status} />
          {fix.risk && <RiskBadge risk={fix.risk} />}
          <span>{SOURCE_LABELS[fix.source]}</span>
          <span>·</span>
          <span>{fix.author ? `@${fix.author}` : "Sentry"}</span>
          <RelativeTime date={fix.created_at} className="text-fg-faint" />
          {prLink(fix.pr_url) && (
            <a
              href={prLink(fix.pr_url) ?? undefined}
              target="_blank"
              rel="noreferrer"
              className="hover:underline"
            >
              Draft pull request<span className="sr-only"> (opens GitHub)</span>
            </a>
          )}
        </div>
        <RunStages kind="fix" stage={fix.stage} status={fix.status} />
      </header>
      <Ended fix={fix} />
      {plan ? (
        <ApprovalCard fix={fix} />
      ) : (
        <section aria-labelledby="report-heading" className="flex flex-col gap-2">
          <h2 id="report-heading" className="text-sm font-medium text-fg-muted">
            The report
          </h2>
          <TracePreview ticket={fix.ticket} />
        </section>
      )}
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <Activity steps={activity.steps} live={activity.live} going={open} />
        <SideQuestion fix={fix} />
      </div>
    </>
  );
}

/** How a fix that stopped short stands: what was tried, and what can be done. */
function Ended({ fix }: { fix: Fix }) {
  if (isOpen(fix.status) || fix.status === "succeeded") return null;
  const repro = reproOf(fix);
  const findings: Finding[] = [
    ...(patchOf(fix)?.findings ?? []),
    ...(verificationOf(fix)?.findings ?? []),
  ];
  return (
    <section
      aria-label="Why it stopped"
      data-testid="ended"
      data-status={fix.status}
      className="flex flex-col gap-3 rounded-md border border-line p-4"
    >
      {fix.stop_reason && (
        <p className="text-sm" data-testid="stop-reason">
          {fix.stop_reason}
        </p>
      )}
      {fix.repro_result === "flaky" && repro && <RunMatrix runs={repro.runs} />}
      {fix.status === "not_reproduced" && fix.repro_result !== "flaky" && repro && (
        <Attempts repro={repro} />
      )}
      {fix.status === "blocked" && findings.length > 0 && (
        <ul className="flex flex-col gap-1 text-sm" data-testid="rule-hits">
          {findings.map((f) => (
            <li key={`${f.rule}:${f.path}:${f.detail}`}>
              <span className="font-medium">{RULE_LABELS[f.rule] ?? f.rule}</span>{" "}
              <span className="font-mono text-xs text-fg-muted">{f.path}</span>
              <code className="mt-0.5 block text-xs text-fg-muted">{f.detail}</code>
            </li>
          ))}
        </ul>
      )}
      {fix.status === "not_reproduced" && <Retry fix={fix} />}
    </section>
  );
}

function Attempts({ repro }: { repro: NonNullable<ReturnType<typeof reproOf>> }) {
  const tried = [...repro.tries, ...repro.runs];
  if (tried.length === 0)
    return <p className="text-xs text-fg-muted">{repro.explanation || "No test was written."}</p>;
  return (
    <details className="text-xs" data-testid="attempts">
      <summary className="cursor-pointer text-fg-muted">
        {tried.length} test run{tried.length === 1 ? "" : "s"}, none failing for the reported reason
      </summary>
      <ol className="mt-2 flex flex-col gap-2">
        {tried.map((run, i) => (
          <li key={i}>
            <span>
              Run {i + 1}: {run.passed ? "passed" : "failed, not for the reported reason"}
            </span>
            <pre className="mt-1 max-h-32 overflow-auto rounded-md bg-surface-2 p-2 whitespace-pre-wrap">
              {run.output || "No output kept."}
            </pre>
          </li>
        ))}
      </ol>
    </details>
  );
}

function RunMatrix({ runs }: { runs: NonNullable<ReturnType<typeof reproOf>>["runs"] }) {
  return (
    <table className="w-fit text-xs" data-testid="run-matrix">
      <caption className="mb-1 text-left text-fg-muted">
        Flaky: the reproducing test, run three times
      </caption>
      <thead>
        <tr>
          {runs.map((_, i) => (
            <th key={i} scope="col" className="px-2 font-normal text-fg-muted">
              Run {i + 1}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        <tr>
          {runs.map((run, i) => (
            <td key={i} className={run.for_reason ? "px-2 text-danger" : "px-2 text-fg-muted"}>
              {run.for_reason ? "failed (the bug)" : run.passed ? "passed" : "failed (other)"}
            </td>
          ))}
        </tr>
      </tbody>
    </table>
  );
}

function Retry({ fix }: { fix: Fix }) {
  const id = useId();
  const { role } = useOrgRole();
  const actions = useFixActions(fix.id);
  const [context, setContext] = useState("");
  const [error, setError] = useState<string | null>(null);
  if (!roleAtLeast(role, "member")) return null;
  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    try {
      await actions.retry(context.trim());
      setContext("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "It couldn't be retried.");
    }
  };
  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-2">
      <label htmlFor={id} className="text-xs font-medium">
        Retry with more context: how to trigger it, the data involved
      </label>
      <Textarea
        id={id}
        rows={3}
        value={context}
        maxLength={4000}
        onChange={(event) => {
          setContext(event.target.value);
        }}
      />
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <div>
        <Button type="submit" size="sm" disabled={actions.busy || !context.trim()}>
          <RotateCcw aria-hidden />
          Retry with this context
        </Button>
      </div>
    </form>
  );
}

function Activity({
  steps,
  live,
  going,
}: {
  steps: { name: string; summary: string; ts: string }[];
  live: boolean;
  going: boolean;
}) {
  return (
    <section
      aria-labelledby="activity-heading"
      className="flex flex-col gap-2"
      data-testid="activity"
    >
      <h2
        id="activity-heading"
        className="flex items-center gap-2 text-sm font-medium text-fg-muted"
      >
        Activity {live && <LiveDot label="Live" />}
      </h2>
      {steps.length === 0 ? (
        <p className="text-xs text-fg-muted">
          {going ? "Waiting for the agents' next step…" : "Its steps are in the run timeline."}
        </p>
      ) : (
        <ol className="flex flex-col gap-1 text-xs" aria-live="polite">
          {steps.map((step, i) => (
            <li key={`${step.ts}:${String(i)}`} className="flex gap-2">
              <span className="w-28 shrink-0 truncate font-mono text-fg-muted">{step.name}</span>
              <span className="min-w-0 truncate">{step.summary}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** A question about the fix, asked in Ask about its root cause's lines. */
function SideQuestion({ fix }: { fix: Fix }) {
  const id = useId();
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  const [question, setQuestion] = useState("");
  const cause = rootCauseOf(fix);
  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    const query = new URLSearchParams({ q: question.trim() });
    if (cause) {
      query.set("scope", cause.path);
      query.set("L", `${String(cause.start_line)}-${String(cause.end_line)}`);
    }
    router.push(`${repoPath(org, repo, "ask")}?${query.toString()}` as never);
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2" data-testid="side-question">
      <label htmlFor={id} className="flex items-center gap-1.5 text-sm font-medium text-fg-muted">
        <CircleHelp aria-hidden className="size-3.5" />
        Ask about this fix
      </label>
      <div className="flex gap-2">
        <Input
          id={id}
          value={question}
          placeholder={cause ? `Who else calls ${cause.path}?` : "What does this code do?"}
          onChange={(event) => {
            setQuestion(event.target.value);
          }}
        />
        <Button type="submit" variant="outline" disabled={!question.trim()}>
          Ask
        </Button>
      </div>
      {cause && <p className="text-xs text-fg-faint">Scoped to {cause.path}.</p>}
    </form>
  );
}
