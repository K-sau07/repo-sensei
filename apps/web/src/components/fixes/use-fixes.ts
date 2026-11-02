"use client";

/** The Fixes tab's reads and writes (8.11): the repository's fixes, one fix, a paste parsed as
 * it is typed, starting a fix, and starting one that waits for triage. */
import { keepPreviousData, useQueryClient } from "@tanstack/react-query";

import { useRepoSlug } from "@/components/repo/use-repo";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { $api } from "@/lib/api/client";
import type { FixStatus } from "@/lib/api/fixes";

const LIST = "/api/v1/orgs/{org}/repos/{repo}/fixes" as const;
const PARSE = "/api/v1/orgs/{org}/repos/{repo}/fixes/parse" as const;
const ONE = "/api/v1/orgs/{org}/fixes/{fix_id}" as const;
const START = "/api/v1/orgs/{org}/fixes/{fix_id}/start" as const;
const SENTRY = "/api/v1/orgs/{org}/integrations/sentry" as const;
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

export function useFix(id: string) {
  const { org } = useRepoSlug();
  return $api.useQuery("get", ONE, { params: { path: { org, fix_id: id } } });
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
