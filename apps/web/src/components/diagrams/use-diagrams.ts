"use client";

/** The Diagrams tab's reads and writes (6.6): the repository's diagrams, one diagram, and
 * drawing, renaming, deleting and sharing one. */
import { useQueryClient } from "@tanstack/react-query";

import { useViewRef } from "@/components/repo/use-ref";
import { useRepoSlug } from "@/components/repo/use-repo";
import { $api } from "@/lib/api/client";

const LIST = "/api/v1/orgs/{org}/repos/{repo}/diagrams" as const;
const ONE = "/api/v1/orgs/{org}/repos/{repo}/diagrams/{diagram_id}" as const;

export function useDiagrams() {
  const { org, repo } = useRepoSlug();
  return $api.useQuery("get", LIST, { params: { path: { org, repo }, query: { limit: 50 } } });
}

export function useDiagram(id: string, enabled = true) {
  const { org, repo } = useRepoSlug();
  return $api.useQuery(
    "get",
    ONE,
    { params: { path: { org, repo, diagram_id: id } } },
    { staleTime: Infinity, enabled: enabled && id !== "" }, // it changes by its title only
  );
}

/** Refreshes the list after a change (and the diagram's own page). */
function useRefresh() {
  const { org, repo } = useRepoSlug();
  const queryClient = useQueryClient();
  return (id?: string) => {
    void queryClient.invalidateQueries({
      queryKey: $api
        .queryOptions("get", LIST, { params: { path: { org, repo } } })
        .queryKey.slice(0, 2),
    });
    if (id) {
      void queryClient.invalidateQueries({
        queryKey: $api.queryOptions("get", ONE, {
          params: { path: { org, repo, diagram_id: id } },
        }).queryKey,
      });
    }
  };
}

export function useCreateDiagram() {
  const refresh = useRefresh();
  const [ref] = useViewRef();
  const create = $api.useMutation("post", LIST, {
    onSuccess: () => {
      refresh();
    },
  });
  const { org, repo } = useRepoSlug();
  return {
    ...create,
    draw: (body: Parameters<typeof create.mutateAsync>[0]["body"]) =>
      create.mutateAsync({
        params: { path: { org, repo }, query: ref ? { ref } : {} },
        body,
      }),
  };
}

export function useDiagramActions(id: string) {
  const { org, repo } = useRepoSlug();
  const refresh = useRefresh();
  const path = { org, repo, diagram_id: id };
  const rename = $api.useMutation("patch", ONE, {
    onSuccess: () => {
      refresh(id);
    },
  });
  const remove = $api.useMutation("delete", ONE, {
    onSuccess: () => {
      refresh();
    },
  });
  const share = $api.useMutation("post", `${ONE}/share`, {
    onSuccess: () => {
      refresh(id);
    },
  });
  const unshare = $api.useMutation("delete", `${ONE}/share`, {
    onSuccess: () => {
      refresh(id);
    },
  });
  return {
    rename: (title: string) => rename.mutateAsync({ params: { path }, body: { title } }),
    remove: () => remove.mutateAsync({ params: { path } }),
    share: () => share.mutateAsync({ params: { path } }),
    unshare: () => unshare.mutateAsync({ params: { path } }),
    busy: rename.isPending || remove.isPending || share.isPending || unshare.isPending,
  };
}
