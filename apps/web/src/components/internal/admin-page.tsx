"use client";

/**
 * The staff admin console (10.6.3): today's spend against the day's cap and the switch that
 * stops anything new that costs (confirmed first), index runs in every org (failed by default)
 * with retry for a failed one and cancel for one under way, and the orgs and people, searched.
 * Retry and cancel act in the run's org as its owner would, and its audit log says who.
 */
import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { parseAsStringLiteral, useQueryState } from "nuqs";
import { useState } from "react";
import { toast } from "sonner";

import { ErrorState } from "@/components/feedback/error-state";
import { StaffOnly } from "@/components/internal/evals-list";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { Schemas } from "@/lib/api/types";

const USD = new Intl.NumberFormat("en", { style: "currency", currency: "USD" });
const WHEN = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });
const STATUSES = ["failed", "running", "queued", "any"] as const;
const STATUS_LABELS: Record<(typeof STATUSES)[number], string> = {
  failed: "Failed",
  running: "Running",
  queued: "Queued",
  any: "Any",
};

export function AdminPage() {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-10 p-8">
      <StaffOnly>
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold">Admin</h1>
          <p className="text-sm text-fg-muted">The service across every org.</p>
        </div>
        <Today />
        <Runs />
        <Orgs />
        <People />
      </StaffOnly>
    </div>
  );
}

function Today() {
  const queryClient = useQueryClient();
  const today = $api.useQuery("get", "/api/v1/internal/admin");
  const key = $api.queryOptions("get", "/api/v1/internal/admin").queryKey;
  const flip = $api.useMutation("put", "/api/v1/internal/admin/read-only", {
    onSuccess: (data) => {
      queryClient.setQueryData(key, data);
    },
  });
  const [asking, setAsking] = useState(false);
  if (today.isError) {
    return <ErrorState title="Today didn't load" onRetry={() => void today.refetch()} />;
  }
  if (!today.data) return <Skeleton className="h-28" role="status" aria-label="Loading today" />;
  const data = today.data;
  const set = async (on: boolean) => {
    try {
      await flip.mutateAsync({ body: { on } });
      toast.success(on ? "New spending stopped until midnight UTC" : "New spending allowed again");
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "The switch didn't change.");
    }
  };
  return (
    <section className="flex flex-col gap-4" aria-labelledby="admin-today">
      <h2 id="admin-today" className="font-medium">
        Today
      </h2>
      <div className="grid gap-4 sm:grid-cols-4">
        <Figure
          title="Spent today"
          value={USD.format(Number(data.spent_today_usd))}
          note={data.cap_usd ? `of ${USD.format(Number(data.cap_usd))} (the day's cap)` : "No cap"}
        />
        <Figure title="Accounts" value={String(data.users)} note="Every person signed up" />
        <Figure title="Active orgs" value={String(data.orgs)} note="Not deleted" />
        <Figure
          title="Failed runs today"
          value={String(data.failed_runs_today)}
          note={`${data.repos} ${data.repos === 1 ? "repository" : "repositories"} in all`}
        />
      </div>
      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-line p-4">
        <p className="min-w-0 flex-1 text-sm">
          {data.read_only ? (
            <>
              <Badge variant="destructive">Read-only</Badge> Nothing new that costs starts until
              midnight UTC. Lifted while today&apos;s spend is past the cap, it comes back within a
              minute.
            </>
          ) : (
            "Asks, diagrams, fixes, changes and index runs start as usual."
          )}
        </p>
        {data.read_only ? (
          <Button
            variant="outline"
            size="sm"
            disabled={flip.isPending}
            onClick={() => void set(false)}
          >
            Allow new spending
          </Button>
        ) : (
          <Button
            variant="destructive"
            size="sm"
            disabled={flip.isPending}
            onClick={() => {
              setAsking(true);
            }}
          >
            Stop new spending
          </Button>
        )}
      </div>
      <AlertDialog open={asking} onOpenChange={setAsking}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Stop new spending everywhere?</AlertDialogTitle>
            <AlertDialogDescription>
              Until midnight UTC, no org can start an ask, a diagram, a fix, a change or an index
              run. Reading what is there goes on.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it running</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => {
                setAsking(false);
                void set(true);
              }}
            >
              Stop new spending
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

function Figure({ title, value, note }: { title: string; value: string; note: string }) {
  return (
    <section className="flex flex-col gap-1 rounded-lg border border-line p-4" aria-label={title}>
      <h3 className="text-sm text-fg-muted">{title}</h3>
      <p className="text-2xl font-semibold tabular-nums">{value}</p>
      <p className="text-xs text-fg-muted">{note}</p>
    </section>
  );
}

function Runs() {
  const queryClient = useQueryClient();
  const [status, setStatus] = useQueryState(
    "runs",
    parseAsStringLiteral(STATUSES).withDefault("failed"),
  );
  const runs = $api.useQuery("get", "/api/v1/internal/admin/jobs", {
    params: { query: { status } },
  });
  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["get", "/api/v1/internal/admin/jobs"] }),
      queryClient.invalidateQueries({ queryKey: ["get", "/api/v1/internal/admin"] }),
    ]);
  const retry = $api.useMutation("post", "/api/v1/internal/admin/jobs/{job}/retry", {
    onSettled: refresh,
  });
  const cancel = $api.useMutation("post", "/api/v1/internal/admin/jobs/{job}/cancel", {
    onSettled: refresh,
  });
  const act = async (what: "retry" | "cancel", job: Schemas["AdminJobOut"]) => {
    try {
      await (what === "retry" ? retry : cancel).mutateAsync({ params: { path: { job: job.id } } });
      toast.success(
        what === "retry"
          ? `${job.repo_full_name} is being indexed again`
          : `${job.repo_full_name}'s run is cancelled`,
      );
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "That didn't go through.");
    }
  };
  return (
    <section className="flex flex-col gap-3" aria-labelledby="admin-runs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="admin-runs" className="font-medium">
          Index runs
        </h2>
        <ToggleGroup
          aria-label="Which runs"
          value={[status]}
          onValueChange={(value: string[]) => {
            const next = STATUSES.find((s) => s === value[0]);
            if (next) void setStatus(next === "failed" ? null : next);
          }}
        >
          {STATUSES.map((s) => (
            <ToggleGroupItem key={s} value={s} size="sm">
              {STATUS_LABELS[s]}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </div>
      {runs.isError ? (
        <ErrorState title="Runs didn't load" onRetry={() => void runs.refetch()} />
      ) : !runs.data ? (
        <Skeleton className="h-32" role="status" aria-label="Loading runs" />
      ) : runs.data.length === 0 ? (
        <p className="text-sm text-fg-muted">No {STATUS_LABELS[status].toLowerCase()} runs.</p>
      ) : (
        <Table aria-label="Index runs">
          <TableHeader>
            <TableRow>
              <TableHead>Repository</TableHead>
              <TableHead>Started</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>What happened</TableHead>
              <TableHead>
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.data.map((job) => (
              <TableRow key={job.id} data-testid="admin-run">
                <TableCell>
                  <span className="font-mono text-xs">{job.repo_full_name}</span>
                  <span className="block text-xs text-fg-muted">{job.org_slug}</span>
                </TableCell>
                <TableCell className="text-sm">{WHEN.format(new Date(job.created_at))}</TableCell>
                <TableCell>
                  {job.status === "failed" ? (
                    <Badge variant="destructive">Failed</Badge>
                  ) : (
                    <Badge variant="outline">{job.status}</Badge>
                  )}
                </TableCell>
                <TableCell className="max-w-sm text-sm text-fg-muted">
                  {job.error && typeof job.error.message === "string"
                    ? `${job.stage ? `${job.stage}: ` : ""}${job.error.message}`
                    : (job.stage ?? "—")}
                </TableCell>
                <TableCell className="text-right">
                  {job.status === "failed" && (
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={retry.isPending}
                      aria-label={`Retry ${job.repo_full_name}`}
                      onClick={() => void act("retry", job)}
                    >
                      Retry
                    </Button>
                  )}
                  {(job.status === "queued" || job.status === "running") && (
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={cancel.isPending}
                      aria-label={`Cancel ${job.repo_full_name}`}
                      onClick={() => void act("cancel", job)}
                    >
                      Cancel
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

function Search({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <Input
      type="search"
      aria-label={label}
      placeholder={label}
      value={value}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      className="h-8 w-64"
    />
  );
}

function Orgs() {
  const [q, setQ] = useState("");
  const orgs = $api.useQuery("get", "/api/v1/internal/admin/orgs", {
    params: { query: q.trim() ? { q: q.trim() } : {} },
  });
  return (
    <section className="flex flex-col gap-3" aria-labelledby="admin-orgs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="admin-orgs" className="font-medium">
          Orgs
        </h2>
        <Search label="Search orgs" value={q} onChange={setQ} />
      </div>
      {orgs.isError ? (
        <ErrorState title="Orgs didn't load" onRetry={() => void orgs.refetch()} />
      ) : !orgs.data ? (
        <Skeleton className="h-32" role="status" aria-label="Loading orgs" />
      ) : (
        <Table aria-label="Orgs">
          <TableHeader>
            <TableRow>
              <TableHead>Org</TableHead>
              <TableHead className="text-right">Members</TableHead>
              <TableHead className="text-right">Repositories</TableHead>
              <TableHead className="text-right">Spent, 30 days</TableHead>
              <TableHead>Since</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {orgs.data.map((org) => (
              <TableRow key={org.id}>
                <TableCell>
                  {org.deleted_at ? (
                    <span>{org.name}</span>
                  ) : (
                    <Link href={`/${org.slug}`} className="hover:underline">
                      {org.name}
                    </Link>
                  )}{" "}
                  <span className="font-mono text-xs text-fg-muted">{org.slug}</span>{" "}
                  {org.deleted_at && <Badge variant="outline">Deleted</Badge>}
                </TableCell>
                <TableCell className="text-right tabular-nums">{org.members}</TableCell>
                <TableCell className="text-right tabular-nums">{org.repos}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {USD.format(Number(org.spent_30d_usd))}
                </TableCell>
                <TableCell className="text-sm">{WHEN.format(new Date(org.created_at))}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

function People() {
  const [q, setQ] = useState("");
  const people = $api.useQuery("get", "/api/v1/internal/admin/users", {
    params: { query: q.trim() ? { q: q.trim() } : {} },
  });
  return (
    <section className="flex flex-col gap-3" aria-labelledby="admin-people">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="admin-people" className="font-medium">
          People
        </h2>
        <Search label="Search people" value={q} onChange={setQ} />
      </div>
      {people.isError ? (
        <ErrorState title="People didn't load" onRetry={() => void people.refetch()} />
      ) : !people.data ? (
        <Skeleton className="h-32" role="status" aria-label="Loading people" />
      ) : (
        <Table aria-label="People">
          <TableHeader>
            <TableRow>
              <TableHead>Login</TableHead>
              <TableHead>Name</TableHead>
              <TableHead className="text-right">Orgs</TableHead>
              <TableHead>Since</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {people.data.map((person) => (
              <TableRow key={person.id}>
                <TableCell>
                  <span className="font-mono text-xs">@{person.login}</span>{" "}
                  {person.is_staff && <Badge variant="secondary">Staff</Badge>}{" "}
                  {person.deleted && <Badge variant="outline">Deleted</Badge>}
                </TableCell>
                <TableCell className="text-sm">{person.name ?? "—"}</TableCell>
                <TableCell className="text-right tabular-nums">{person.orgs}</TableCell>
                <TableCell className="text-sm">
                  {WHEN.format(new Date(person.created_at))}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  );
}
