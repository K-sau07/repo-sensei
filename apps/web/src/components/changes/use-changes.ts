"use client";

/** The Changes tab's reads and writes (9.3): the repository's changes, one change (followed
 * while it moves), starting one, editing its plan, deciding on it, cancelling it. */
import { keepPreviousData, useQueryClient } from "@tanstack/react-query";

import { useRepoSlug } from "@/components/repo/use-repo";
import { $api } from "@/lib/api/client";
import { isOpen, type FixStatus } from "@/lib/api/fixes";

const LIST = "/api/v1/orgs/{org}/repos/{repo}/changes" as const;
const ONE = "/api/v1/orgs/{org}/changes/{fix_id}" as const;
const PLAN = "/api/v1/orgs/{org}/changes/{fix_id}/plan" as const;
const DECIDE = "/api/v1/orgs/{org}/changes/{fix_id}/approval" as const;
const CANCEL = "/api/v1/orgs/{org}/changes/{fix_id}/cancel" as const;
const FOLLOW_MS = 1_500; // while a stage works, the page reads the change this often

export function useChanges(status: FixStatus | null = null) {
  const { org, repo } = useRepoSlug();
  return $api.useQuery(
    "get",
    LIST,
    { params: { path: { org, repo }, query: status ? { status } : {} } },
    { placeholderData: keepPreviousData, refetchInterval: 15_000 },
  );
}

/** One change, read again while a stage works on it (waiting for people, or over: once). */
export function useChange(id: string) {
  const { org } = useRepoSlug();
  return $api.useQuery(
    "get",
    ONE,
    { params: { path: { org, fix_id: id } } },
    {
      refetchInterval: (query) => {
        const status = query.state.data?.status;
        return status && isOpen(status) && status !== "awaiting_approval" ? FOLLOW_MS : false;
      },
    },
  );
}

function useRefresh(id?: string) {
  const { org, repo } = useRepoSlug();
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({
      queryKey: $api
        .queryOptions("get", LIST, { params: { path: { org, repo } } })
        .queryKey.slice(0, 2),
    });
    if (id) {
      void queryClient.invalidateQueries({
        queryKey: $api.queryOptions("get", ONE, { params: { path: { org, fix_id: id } } }).queryKey,
      });
    }
  };
}

export function useCreateChange() {
  const { org, repo } = useRepoSlug();
  const refresh = useRefresh();
  const create = $api.useMutation("post", LIST, { onSuccess: refresh });
  return {
    ...create,
    start: (body: Parameters<typeof create.mutateAsync>[0]["body"]) =>
      create.mutateAsync({ params: { path: { org, repo } }, body }),
  };
}

/** Editing the plan, deciding on it, cancelling the change. */
export function useChangeActions(id: string) {
  const { org } = useRepoSlug();
  const refresh = useRefresh(id);
  const path = { org, fix_id: id };
  const edit = $api.useMutation("patch", PLAN, { onSuccess: refresh });
  const decide = $api.useMutation("post", DECIDE, { onSuccess: refresh });
  const cancel = $api.useMutation("post", CANCEL, { onSuccess: refresh });
  return {
    edit: (body: Parameters<typeof edit.mutateAsync>[0]["body"]) =>
      edit.mutateAsync({ params: { path }, body }),
    decide: (body: Parameters<typeof decide.mutateAsync>[0]["body"]) =>
      decide.mutateAsync({ params: { path }, body }),
    cancel: () => cancel.mutateAsync({ params: { path } }),
    busy: edit.isPending || decide.isPending || cancel.isPending,
  };
}
