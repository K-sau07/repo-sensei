"use client";

/** The Fixes tab's reads and writes (8.11): the repository's fixes, one fix, a paste parsed as
 * it is typed, starting a fix, and starting one that waits for triage. */
import { keepPreviousData, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { useRepoSlug } from "@/components/repo/use-repo";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { $api } from "@/lib/api/client";
import { isOpen, WAITING_MS, type FixStatus } from "@/lib/api/fixes";
import { followStream } from "@/lib/event-stream";

const LIST = "/api/v1/orgs/{org}/repos/{repo}/fixes" as const;
const PARSE = "/api/v1/orgs/{org}/repos/{repo}/fixes/parse" as const;
const ONE = "/api/v1/orgs/{org}/fixes/{fix_id}" as const;
const START = "/api/v1/orgs/{org}/fixes/{fix_id}/start" as const;
const SENTRY = "/api/v1/orgs/{org}/integrations/sentry" as const;
const APPROVALS = "/api/v1/orgs/{org}/fixes/{fix_id}/approvals" as const;
const DECIDE = "/api/v1/orgs/{org}/fixes/{fix_id}/approval" as const;
const RETRY = "/api/v1/orgs/{org}/fixes/{fix_id}/retry" as const;
const CANCEL = "/api/v1/orgs/{org}/fixes/{fix_id}/cancel" as const;
const FOLLOW_MS = 2_000;
const MAX_STEPS = 50;
const ENDS = new Set(["done", "not_reproduced", "blocked", "failed", "rejected", "cancelled"]);
const FIX_EVENTS = [
  "queued", "stage_started", "step", "stage_done", "awaiting_approval", "approval", "approved",
  "replanning", "retrying", ...ENDS,
] as const; // prettier-ignore
const PARSE_DELAY_MS = 400;
const MIN_PARSE_CHARS = 8;

export function useFixes(status: FixStatus | null = null) {
  const { org, repo } = useRepoSlug();
  return $api.useQuery(
    "get",
    LIST,
    { params: { path: { org, repo }, query: status ? { status } : {} } },
    { placeholderData: keepPreviousData, refetchInterval: 15_000 }, // runs move on their own
  );
}

/** One fix, read again while a stage works on it, less often while its plan waits for people
 * (who may decide meanwhile), never once it's over or waiting for triage. */
export function useFix(id: string) {
  const { org } = useRepoSlug();
  return $api.useQuery(
    "get",
    ONE,
    { params: { path: { org, fix_id: id } } },
    {
      refetchInterval: (query) => {
        const status = query.state.data?.status;
        if (!status || !isOpen(status) || status === "needs_triage") return false;
        return status === "awaiting_approval" ? WAITING_MS : FOLLOW_MS;
      },
    },
  );
}

/** Who decided on the plan; read again while it waits (``waiting``). */
export function useFixApprovals(id: string, waiting = false) {
  const { org } = useRepoSlug();
  return $api.useQuery(
    "get",
    APPROVALS,
    { params: { path: { org, fix_id: id } } },
    { refetchInterval: waiting ? WAITING_MS : false },
  );
}

export interface LiveStep {
  name: string;
  summary: string;
  ts: string;
}

/** The fix's steps as they are recorded (its event stream), newest last, while it runs. */
export function useFixActivity(id: string, open: boolean): { steps: LiveStep[]; live: boolean } {
  const { org } = useRepoSlug();
  const queryClient = useQueryClient();
  const [steps, setSteps] = useState<LiveStep[]>([]);
  const [live, setLive] = useState(false);
  const last = useRef<string | null>(null);
  const done = useRef(false);
  useEffect(() => {
    if (!open) return;
    done.current = false;
    const url = `/api/v1/orgs/${encodeURIComponent(org)}/fixes/${encodeURIComponent(id)}/events`;
    return followStream(url, {
      types: [...FIX_EVENTS],
      onLive: setLive,
      lastId: () => last.current,
      finished: () => done.current,
      onEvent: (message) => {
        if (message.lastEventId) last.current = message.lastEventId;
        const data = JSON.parse(message.data) as { type: string; name?: string; summary?: string;
                                                   ts?: string }; // prettier-ignore
        if (data.type === "step") {
          const step = { name: data.name ?? "", summary: data.summary ?? "", ts: data.ts ?? "" };
          setSteps((held) => [...held.slice(-(MAX_STEPS - 1)), step]);
          return;
        }
        // A move (a stage begun or done, waiting for approval, an end): the fix read again.
        void queryClient.invalidateQueries({
          queryKey: $api.queryOptions("get", ONE, { params: { path: { org, fix_id: id } } })
            .queryKey,
        });
        if (ENDS.has(data.type)) {
          done.current = true;
          setLive(false);
        }
      },
    });
  }, [open, org, id, queryClient]);
  return { steps, live };
}

/** Deciding on the plan, retrying with more context, cancelling. */
export function useFixActions(id: string) {
  const { org } = useRepoSlug();
  const queryClient = useQueryClient();
  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: $api.queryOptions("get", ONE, { params: { path: { org, fix_id: id } } }).queryKey,
    });
    void queryClient.invalidateQueries({
      queryKey: $api.queryOptions("get", APPROVALS, { params: { path: { org, fix_id: id } } })
        .queryKey,
    });
  };
  const path = { org, fix_id: id };
  // Read again however it went: a refusal (someone decided first, the plan changed) says what's
  // shown is out of date.
  const decide = $api.useMutation("post", DECIDE, { onSettled: refresh });
  const retry = $api.useMutation("post", RETRY, { onSettled: refresh });
  const cancel = $api.useMutation("post", CANCEL, { onSettled: refresh });
  return {
    decide: (body: Parameters<typeof decide.mutateAsync>[0]["body"]) =>
      decide.mutateAsync({ params: { path }, body }),
    retry: (context: string) => retry.mutateAsync({ params: { path }, body: { context } }),
    cancel: () => cancel.mutateAsync({ params: { path } }),
    busy: decide.isPending || retry.isPending || cancel.isPending,
  };
}

/** What the API makes of ``text`` (its frames, error, versions), once typing pauses. */
export function useParsedTrace(text: string) {
  const { org, repo } = useRepoSlug();
  const settled = useDebouncedValue(text.trim(), PARSE_DELAY_MS);
  const parse = $api.useQuery(
    "post",
    PARSE,
    { params: { path: { org, repo } }, body: { text: settled } },
    { enabled: settled.length >= MIN_PARSE_CHARS, placeholderData: keepPreviousData },
  );
  return { ...parse, waiting: settled !== text.trim() || parse.isFetching };
}

/** The org's Sentry integration (null: not connected). */
export function useSentry() {
  const { org } = useRepoSlug();
  return $api.useQuery("get", SENTRY, { params: { path: { org } } }, { retry: false });
}

function useRefresh() {
  const { org, repo } = useRepoSlug();
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({
      queryKey: $api
        .queryOptions("get", LIST, { params: { path: { org, repo } } })
        .queryKey.slice(0, 2),
    });
  };
}

export function useCreateFix() {
  const { org, repo } = useRepoSlug();
  const refresh = useRefresh();
  const create = $api.useMutation("post", LIST, { onSuccess: refresh });
  return {
    ...create,
    start: (body: Parameters<typeof create.mutateAsync>[0]["body"]) =>
      create.mutateAsync({ params: { path: { org, repo } }, body }),
  };
}

export function useStartFix() {
  const { org } = useRepoSlug();
  const refresh = useRefresh();
  const start = $api.useMutation("post", START, { onSuccess: refresh });
  return {
    ...start,
    begin: (id: string) => start.mutateAsync({ params: { path: { org, fix_id: id } } }),
  };
}
