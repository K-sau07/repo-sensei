"use client";

/**
 * An org's security settings (admins, 10.7.2): its audit log, newest first, filtered by what
 * was done and by whom, a page at a time.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api, api } from "@/lib/api/client";
import {
  actionLabel,
  actorLabel,
  AUDIT_ACTIONS,
  targetLabel,
  type AuditAction,
} from "@/lib/orgs/audit";

const PAGE = 50;
const ANY = "any";
const WHEN = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });

export function SecuritySettings() {
  const { slug, role } = useOrgRole();
  if (role === null) {
    return <Skeleton className="h-40" role="status" aria-label="Loading" />;
  }
  if (!roleAtLeast(role, "admin")) {
    return (
      <p className="text-sm text-fg-muted">
        Only the organization&apos;s admins read its audit log.
      </p>
    );
  }
  return <AuditLog org={slug} />;
}

function AuditLog({ org }: { org: string }) {
  const [action, setAction] = useState<AuditAction | null>(null);
  const [actor, setActor] = useState<string | null>(null);
  const members = $api.useQuery("get", "/api/v1/orgs/{org}/members", {
    params: { path: { org } },
  });
  const log = useInfiniteQuery({
    queryKey: ["audit", org, action, actor],
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) => {
      const { data } = await api.GET("/api/v1/orgs/{org}/audit", {
        params: {
          path: { org },
          query: {
            limit: PAGE,
            ...(action ? { action } : {}),
            ...(actor ? { actor } : {}),
            ...(pageParam ? { before: pageParam } : {}),
          },
        },
      });
      if (!data) throw new Error("The audit log didn't load.");
      return data;
    },
    getNextPageParam: (last) => last.next_cursor ?? null,
  });
  const events = log.data?.pages.flatMap((page) => page.items) ?? [];
  const who = members.data?.find((m) => m.user_id === actor);

  return (
    <section className="flex flex-col gap-4" aria-labelledby="audit-title">
      <div className="flex flex-col gap-1">
        <h2 id="audit-title" className="font-medium">
          Audit log
        </h2>
        <p className="text-sm text-fg-muted">
          What was done in this organization, by whom and from where, newest first.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Select
          value={action ?? ANY}
          onValueChange={(value) => {
            setAction(value === ANY || value === null ? null : (value as AuditAction));
          }}
        >
          <SelectTrigger aria-label="What was done" size="sm" className="w-64">
            <SelectValue>{action ? actionLabel(action) : "Everything"}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>Everything</SelectItem>
            {AUDIT_ACTIONS.map((value) => (
              <SelectItem key={value} value={value}>
                {actionLabel(value)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={actor ?? ANY}
          onValueChange={(value) => {
            setActor(value === ANY || value === null ? null : value);
          }}
        >
          <SelectTrigger aria-label="Who did it" size="sm" className="w-52">
            <SelectValue>{who ? `@${who.login}` : "Anyone"}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>Anyone</SelectItem>
            {(members.data ?? []).map((m) => (
              <SelectItem key={m.user_id} value={m.user_id}>
                @{m.login}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {log.isError ? (
        <ErrorState title="The audit log didn't load" onRetry={() => void log.refetch()} />
      ) : log.isPending ? (
        <div role="status" aria-label="Loading the audit log" className="flex flex-col gap-2">
          <Skeleton className="h-row" />
          <Skeleton className="h-row" />
          <Skeleton className="h-row" />
        </div>
      ) : events.length === 0 ? (
        <EmptyState title="Nothing recorded" description="No event matches these filters." />
      ) : (
        <>
          <Table aria-label="Audit log">
            <TableHeader>
              <TableRow>
                <TableHead>When</TableHead>
                <TableHead>Who</TableHead>
                <TableHead>What</TableHead>
                <TableHead>About</TableHead>
                <TableHead>From</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((event) => (
                <TableRow key={event.id}>
                  <TableCell className="whitespace-nowrap text-fg-muted">
                    <time dateTime={event.created_at}>
                      {WHEN.format(new Date(event.created_at))}
                    </time>
                  </TableCell>
                  <TableCell>{actorLabel(event)}</TableCell>
                  <TableCell>{actionLabel(event.action)}</TableCell>
                  <TableCell className="font-mono text-xs">{targetLabel(event) ?? ""}</TableCell>
                  <TableCell className="font-mono text-xs text-fg-muted">
                    {event.ip ?? ""}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {log.hasNextPage && (
            <div>
              <Button
                type="button"
                variant="outline"
                disabled={log.isFetchingNextPage}
                onClick={() => void log.fetchNextPage()}
              >
                Older events
              </Button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
