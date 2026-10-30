"use client";

/**
 * A repository's agent runs, and one run (4.12): its detail, and while it runs, its steps as
 * they are recorded (the run's event stream), appended to the ones read; once it finishes, the
 * detail again (with the steps' outputs).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { useRepoSlug } from "@/components/repo/use-repo";
import { api } from "@/lib/api/client";
import type { Step } from "@/lib/api/runs";
import { followStream } from "@/lib/event-stream";
import { queryKeys } from "@/lib/query/keys";

export function useRuns() {
  const { org, repo } = useRepoSlug();
  return useQuery({
    queryKey: queryKeys.runs(org, repo),
    queryFn: async ({ signal }) => {
      const { data } = await api.GET("/api/v1/orgs/{org}/repos/{repo}/runs", {
        params: { path: { org, repo }, query: { limit: 50 } },
        signal,
      });
      if (!data) throw new Error("no runs");
      return data;
    },
  });
}

export function useRun(runId: string) {
  const { org } = useRepoSlug();
  const queryClient = useQueryClient();
  const key = queryKeys.run(org, runId);
  const detail = useQuery({
    queryKey: key,
    queryFn: async ({ signal }) => {
      const { data } = await api.GET("/api/v1/orgs/{org}/agent-runs/{run_id}", {
        params: { path: { org, run_id: runId } },
        signal,
      });
      if (!data) throw new Error("no run");
      return data;
    },
  });
  const running = detail.data?.finished_at === null;
  const [streamed, setStreamed] = useState<Step[]>([]);
  const [live, setLive] = useState(false);
  const last = useRef<string | null>(null);
  const done = useRef(false);

  useEffect(() => {
    if (!running) return;
    done.current = false;
    const url = `/api/v1/orgs/${encodeURIComponent(org)}/agent-runs/${encodeURIComponent(runId)}/events`;
    return followStream(url, {
      types: ["step", "finished"],
      onLive: setLive,
      lastId: () => last.current,
      finished: () => done.current,
      onEvent: (message) => {
        if (message.lastEventId) last.current = message.lastEventId;
        const data = JSON.parse(message.data) as { type: string } & Partial<Step>;
        if (data.type === "finished") {
          done.current = true;
          setLive(false);
          void queryClient.invalidateQueries({ queryKey: key });
          return;
        }
        const { type: _, ...step } = data;
        setStreamed((held) => [...held, step as Step]);
      },
    });
    // `key` is built from these two.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, org, runId, queryClient]);

  const read = detail.data?.steps ?? [];
  const seen = new Set(read.map((s) => s.idx));
  const steps: Step[] = [...read, ...streamed.filter((s) => !seen.has(s.idx))].sort(
    (a, b) => a.idx - b.idx,
  );
  return { ...detail, steps, live: running && live };
}
