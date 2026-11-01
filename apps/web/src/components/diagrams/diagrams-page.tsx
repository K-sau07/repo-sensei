"use client";

/**
 * `/[org]/[repo]/diagrams` (6.6): the repository's diagrams, newest first, each with how much of
 * it the code graph proves; and "New diagram": a question ("How does login work?") or, from a
 * trace's "Open as diagram" (``?from=`` its entry, ``&path=`` which path), its flow, drawn from
 * the graph and opened.
 */
import { ShieldCheck, Workflow } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { LockedState } from "@/components/feedback/locked-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  DIAGRAM_KINDS,
  KIND_LABELS,
  verifiedPercent,
  type DiagramInput,
  type DiagramKind,
} from "@/lib/api/diagrams";
import { ApiError } from "@/lib/api/errors";
import { STAGE_LABELS } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";

import { useCreateDiagram, useDiagrams } from "./use-diagrams";

export function DiagramsPage() {
  const { org, repo } = useRepoSlug();
  const { state, ready, active } = useRepoFeatures();
  if (!ready) return null;
  const diagrams = state("diagrams");
  if (!diagrams.available) {
    const stage = diagrams.stage ? STAGE_LABELS[diagrams.stage] : "indexing";
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col p-8">
        {active && diagrams.coming ? (
          <LockedState
            feature="Diagrams"
            stage={stage}
            {...(diagrams.etaSeconds !== null ? { etaSeconds: diagrams.etaSeconds } : {})}
            href={repoPath(org, repo)}
          />
        ) : (
          <EmptyState
            icon={<Workflow aria-hidden />}
            title="Diagrams open after indexing"
            description="A diagram is drawn from the code graph, once its links are indexed."
          />
        )}
      </div>
    );
  }
  return <Diagrams />;
}

function Diagrams() {
  const { org, repo } = useRepoSlug();
  const list = useDiagrams();
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-6">
      <NewDiagram />
      <section aria-labelledby="diagrams-heading" className="flex flex-col gap-2">
        <h2 id="diagrams-heading" className="text-sm font-medium text-fg-muted">
          Diagrams
        </h2>
        {list.isPending ? (
          <div role="status" aria-label="Loading the diagrams" className="flex flex-col gap-2">
            <Skeleton className="h-12" />
            <Skeleton className="h-12" />
          </div>
        ) : list.isError ? (
          <ErrorState title="Couldn't load the diagrams" onRetry={() => void list.refetch()} />
        ) : list.data.diagrams.length === 0 ? (
          <p className="text-sm text-fg-faint" data-testid="no-diagrams">
            None yet. Ask for a flow above, or open one from a trace.
          </p>
        ) : (
          <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
            {list.data.diagrams.map((d) => (
              <li key={d.id}>
                <Link
                  href={repoPath(org, repo, `diagrams/${d.id}`)}
                  className="flex items-center gap-3 px-3 py-2.5 hover:bg-surface-2"
                >
                  <Workflow aria-hidden className="size-4 text-fg-muted" />
                  <span className="min-w-0 flex-1 truncate text-sm">{d.title}</span>
                  <span className="text-xs text-fg-muted">{KIND_LABELS[d.kind]}</span>
                  <span className="text-xs text-fg-muted tabular-nums">
                    {d.nodes} steps · {d.edges} edges
                  </span>
                  <span className="flex items-center gap-1 text-xs text-ok tabular-nums">
                    <ShieldCheck aria-hidden className="size-3.5" />
                    {verifiedPercent(d.verified_ratio)}
                    <span className="sr-only"> statically verified</span>
                  </span>
                  <RelativeTime date={d.created_at} className="text-xs text-fg-faint" />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function NewDiagram() {
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  const search = useSearchParams();
  const create = useCreateDiagram();
  const [question, setQuestion] = useState("");
  const [kind, setKind] = useState<DiagramKind>("flowchart");
  const [error, setError] = useState<string | null>(null);
  const draw = async (input: DiagramInput) => {
    setError(null);
    try {
      const made = await create.draw(input);
      router.push(repoPath(org, repo, `diagrams/${made.id}`));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The diagram couldn't be drawn.");
    }
  };
  // A trace's "Open as diagram": its flow, drawn once, the address left clean.
  const started = useRef(false);
  const from = search.get("from");
  const path = Number(search.get("path") ?? "1");
  useEffect(() => {
    if (!from || started.current) return;
    started.current = true;
    window.history.replaceState(null, "", window.location.pathname);
    void draw({
      from_trace: {
        from,
        direction: "both",
        kind: "auto",
        path: Number.isInteger(path) && path >= 1 && path <= 3 ? path : 1,
      },
      kind: "sequence",
    });
    // Once, for the entry in the address.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [from]);

  return (
    <form
      aria-label="Draw a diagram"
      className="flex flex-col gap-2 rounded-lg border border-line p-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (question.trim()) void draw({ question: question.trim(), kind });
      }}
    >
      <label htmlFor="diagram-question" className="text-sm font-medium">
        New diagram
      </label>
      <Textarea
        id="diagram-question"
        placeholder="Show the flow of creating an item"
        value={question}
        maxLength={2000}
        onChange={(event) => {
          setQuestion(event.target.value);
        }}
        className="min-h-16"
      />
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={kind}
          onValueChange={(value) => {
            if (value) setKind(value);
          }}
        >
          <SelectTrigger className="w-40" aria-label="Kind">
            <SelectValue>{KIND_LABELS[kind]}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {DIAGRAM_KINDS.map((k) => (
              <SelectItem key={k} value={k}>
                {KIND_LABELS[k]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button type="submit" disabled={create.isPending || !question.trim()}>
          {create.isPending ? "Drawing from the graph…" : "Draw"}
        </Button>
        <p className="text-xs text-fg-faint">
          Drawn from the code graph: every step is real code; an edge the graph can&apos;t prove is
          dashed, with why.
        </p>
      </div>
      {error ? (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      ) : null}
    </form>
  );
}
