"use client";

/**
 * `/[org]/[repo]/fixes/new` (8.11): a fix from what people have.
 *
 * - **Paste trace**: a stack trace (or the bug in words), parsed as it is typed: its error and
 *   its frames as `file:line` chips, the repository's opening the code at that line;
 * - **GitHub issue**: its number or URL (read with the user's own GitHub account: its body and
 *   comments, the trace found in them);
 * - **Sentry**: the issues Sentry sent that wait for triage (busy ones start by themselves when
 *   the org turned that on), each started from here.
 *
 * "Start investigation" opens the fix's run. Viewers can't start fixes: the page says so.
 */
import { Bug, CircleDot, Radio } from "lucide-react";
import { useRouter } from "next/navigation";
import { parseAsStringLiteral, useQueryState } from "nuqs";
import { useId, useState, type SubmitEvent } from "react";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { PermissionState } from "@/components/feedback/permission-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { ApiError } from "@/lib/api/errors";
import { looksLikeIssue } from "@/lib/api/fixes";
import { repoPath } from "@/lib/routes";

import { TracePreview } from "./trace-preview";
import { useCreateFix, useFixes, useParsedTrace, useSentry, useStartFix } from "./use-fixes";

const TABS = ["paste", "issue", "sentry"] as const;
const MAX_TEXT = 100_000;

function said(caught: unknown, fallback: string): string {
  return caught instanceof ApiError ? caught.message : fallback;
}

export function NewFixPage() {
  const { role, me } = useOrgRole();
  const [tab, setTab] = useQueryState(
    "tab",
    parseAsStringLiteral(TABS).withDefault("paste").withOptions({ history: "replace" }),
  );
  if (me.isPending) return null;
  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 p-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold">New fix</h1>
        <p className="text-sm text-fg-muted">
          A fix first reproduces the bug with a failing test. Nothing is changed until a person
          approves the plan, and the result is a draft pull request, never a merge.
        </p>
      </div>
      {!roleAtLeast(role, "member") ? (
        <PermissionState
          requiredRole="member"
          {...(role ? { yourRole: role } : {})}
          grantedBy="An org owner or admin"
        />
      ) : (
        <Tabs
          value={tab}
          onValueChange={(value) => void setTab(value as (typeof TABS)[number])}
          className="flex flex-col gap-4"
        >
          <TabsList>
            <TabsTrigger value="paste">Paste trace</TabsTrigger>
            <TabsTrigger value="issue">GitHub issue</TabsTrigger>
            <TabsTrigger value="sentry">Sentry</TabsTrigger>
          </TabsList>
          <TabsContent value="paste">
            <PasteTab />
          </TabsContent>
          <TabsContent value="issue">
            <IssueTab />
          </TabsContent>
          <TabsContent value="sentry">
            <SentryTab admin={roleAtLeast(role, "admin")} />
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}

function useOpenFix() {
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  return (id: string) => {
    router.push(repoPath(org, repo, `fixes/${id}`));
  };
}

function PasteTab() {
  const ids = { text: useId(), title: useId() };
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const parsed = useParsedTrace(text);
  const create = useCreateFix();
  const open = useOpenFix();
  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    try {
      const made = await create.start({
        source: "paste",
        text,
        ...(title.trim() ? { title: title.trim() } : {}),
      });
      open(made.id);
    } catch (caught) {
      setError(said(caught, "The fix couldn't be started."));
    }
  };
  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={ids.text}>Stack trace or what goes wrong</Label>
        <Textarea
          id={ids.text}
          value={text}
          maxLength={MAX_TEXT}
          onChange={(event) => {
            setText(event.target.value);
          }}
          rows={10}
          className="font-mono text-xs"
          placeholder={
            'Traceback (most recent call last):\n  File "app/stats.py", line 2, in mean\nZeroDivisionError: division by zero'
          }
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={ids.title}>Title (optional)</Label>
        <Input
          id={ids.title}
          value={title}
          maxLength={200}
          onChange={(event) => {
            setTitle(event.target.value);
          }}
          placeholder="The error, by default"
        />
      </div>
      {text.trim().length > 0 && (
        <div aria-live="polite">
          {parsed.data ? (
            <TracePreview ticket={parsed.data} className={parsed.waiting ? "opacity-70" : ""} />
          ) : parsed.isError ? (
            <p className="text-xs text-fg-muted">
              The preview couldn&apos;t be read; you can still start.
            </p>
          ) : (
            <p className="text-xs text-fg-faint">Reading the trace…</p>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <div>
        <Button type="submit" disabled={create.isPending || !text.trim()}>
          <Bug aria-hidden />
          {create.isPending ? "Starting…" : "Start investigation"}
        </Button>
      </div>
    </form>
  );
}

function IssueTab() {
  const id = useId();
  const [issue, setIssue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const create = useCreateFix();
  const open = useOpenFix();
  const valid = looksLikeIssue(issue);
  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    try {
      const made = await create.start({ source: "github_issue", issue: issue.trim() });
      open(made.id); // an issue already being fixed opens that fix
    } catch (caught) {
      setError(said(caught, "The issue couldn't be read."));
    }
  };
  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={id}>Issue number or URL</Label>
        <Input
          id={id}
          value={issue}
          onChange={(event) => {
            setIssue(event.target.value);
          }}
          placeholder="#123 or https://github.com/owner/repo/issues/123"
          aria-invalid={issue.trim() !== "" && !valid}
        />
        <p className="text-xs text-fg-muted">
          Its body and comments are read with your GitHub account; a stack trace in them is parsed
          like a paste. An issue already being fixed opens that fix.
        </p>
      </div>
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <div>
        <Button type="submit" disabled={create.isPending || !valid}>
          <CircleDot aria-hidden />
          {create.isPending ? "Reading the issue…" : "Start investigation"}
        </Button>
      </div>
    </form>
  );
}

function SentryTab({ admin }: { admin: boolean }) {
  const waiting = useFixes("needs_triage");
  const start = useStartFix();
  const open = useOpenFix();
  const [error, setError] = useState<string | null>(null);
  const issues = (waiting.data ?? []).filter((fix) => fix.source === "sentry");
  const begin = async (id: string) => {
    setError(null);
    try {
      await start.begin(id);
      open(id);
    } catch (caught) {
      setError(said(caught, "The fix couldn't be started."));
    }
  };
  if (waiting.isPending) return <Skeleton className="h-24" />;
  if (waiting.isError)
    return (
      <ErrorState title="Couldn't load the Sentry issues" onRetry={() => void waiting.refetch()} />
    );
  return (
    <div className="flex flex-col gap-3">
      {admin && <SentryStatus />}
      {issues.length === 0 ? (
        <EmptyState
          icon={<Radio aria-hidden />}
          title="No Sentry issues waiting"
          description="Issues Sentry sends land here for triage; busy ones start by themselves when the org turned that on."
        />
      ) : (
        <ul
          className="flex flex-col divide-y divide-line rounded-md border border-line"
          aria-label="Sentry issues waiting for triage"
        >
          {issues.map((fix) => (
            <li
              key={fix.id}
              className="flex items-center gap-3 px-3 py-2.5"
              data-testid="sentry-issue"
            >
              <Radio aria-hidden className="size-4 text-fg-muted" />
              <span className="min-w-0 flex-1 truncate text-sm">{fix.title}</span>
              <RelativeTime date={fix.created_at} className="text-xs text-fg-faint" />
              <Button
                size="sm"
                variant="outline"
                disabled={start.isPending}
                onClick={() => void begin(fix.id)}
                aria-label={`Start investigating ${fix.title}`}
              >
                Start investigation
              </Button>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

/** Whether the org's Sentry is connected (admins only: its settings are theirs). */
function SentryStatus() {
  const sentry = useSentry();
  if (sentry.isPending) return null;
  if (sentry.data) {
    const projects = Object.keys(sentry.data.projects).length;
    return (
      <p className="text-xs text-fg-muted" data-testid="sentry-status">
        Connected to {sentry.data.sentry_org} ({String(projects)} project{projects === 1 ? "" : "s"}
        );{" "}
        {sentry.data.auto_run
          ? `issues with ${String(sentry.data.min_events)}+ events in 14 days start by themselves.`
          : "every issue waits here for triage."}
      </p>
    );
  }
  return (
    <p className="text-xs text-fg-muted" data-testid="sentry-status">
      Sentry isn&apos;t connected. An admin connects it with a Sentry internal integration&apos;s
      token and webhook secret; its issues then arrive here.
    </p>
  );
}
