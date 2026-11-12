"use client";

/**
 * `/[org]/[repo]/diagrams` (6.6): the whole app's architecture first (how the repository is
 * built: its modules by tier and how they depend on each other, from the graph alone, free);
 * the repository's diagrams, newest first, each with how much of it the code graph proves; and
 * "New diagram": a question ("How does login work?") or, from a trace's "Open as diagram"
 * (``?from=`` its entry, ``&path=`` which path), its flow, drawn from the graph and opened.
 */
import { Network, ShieldCheck, Table2, Workflow } from "lucide-react";
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
      <WholeApp />
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

/** The whole app, a click away: its architecture (its parts named by the model, every line the
 * graph's) and its schema (an ER diagram of its tables, from the graph alone: nothing spent). */
function WholeApp() {
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  const search = useSearchParams();
  const create = useCreateDiagram();
  const [error, setError] = useState<string | null>(null);
  const [drawing, setDrawing] = useState<"app" | "schema" | null>(null);
  const draw = async (what: "app" | "schema") => {
    setError(null);
    setDrawing(what);
    try {
      const made = await create.draw(
        what === "app"
          ? { whole_app: true, kind: "architecture" }
          : { whole_schema: true, kind: "er" },
      );
      router.push(repoPath(org, repo, `diagrams/${made.id}`));
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : what === "app"
            ? "The architecture couldn't be drawn."
            : "The schema couldn't be drawn.",
      );
    } finally {
      setDrawing(null);
    }
  };
  // The overview's "Open as an architecture diagram" (``?whole=1``): drawn once, the address
  // left clean.
  const started = useRef(false);
  const asked = search.get("whole") === "1";
  useEffect(() => {
    if (!asked || started.current) return;
    started.current = true;
    window.history.replaceState(null, "", window.location.pathname);
    void draw("app");
    // Once, for the address.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asked]);
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <section
        aria-labelledby="whole-app-heading"
        className="flex flex-col gap-3 rounded-lg border border-line bg-surface-1 p-4"
      >
        <div className="flex items-start gap-3">
          <Network aria-hidden className="size-7 shrink-0 text-signal" />
          <div className="flex min-w-0 flex-col gap-0.5">
            <h2 id="whole-app-heading" className="text-sm font-medium">
              The whole app&apos;s architecture
            </h2>
            <p className="text-xs text-fg-muted">
              How it is built, as you&apos;d draw it on a whiteboard: who uses it, its frontend, API
              and services, the databases and services it uses. AI names the parts; every line
              between them is the code&apos;s own.
            </p>
          </div>
        </div>
        <Button className="self-start" disabled={create.isPending} onClick={() => void draw("app")}>
          {drawing === "app" ? "Mapping…" : "Map the architecture"}
        </Button>
      </section>
      <section
        aria-labelledby="whole-schema-heading"
        className="flex flex-col gap-3 rounded-lg border border-line bg-surface-1 p-4"
      >
        <div className="flex items-start gap-3">
          <Table2 aria-hidden className="size-7 shrink-0 text-layer-db" />
          <div className="flex min-w-0 flex-col gap-0.5">
            <h2 id="whole-schema-heading" className="text-sm font-medium">
              The data model (ER diagram)
            </h2>
            <p className="text-xs text-fg-muted">
              Every table with its columns, keys and types, and how they relate, as the models and
              migrations declare them. Read from the code: free, and exact.
            </p>
          </div>
        </div>
        <Button
          className="self-start"
          variant="outline"
          disabled={create.isPending}
          onClick={() => void draw("schema")}
        >
          {drawing === "schema" ? "Mapping…" : "Map the schema"}
        </Button>
      </section>
      {error ? (
        <p role="alert" className="text-sm text-danger md:col-span-2">
          {error}
        </p>
      ) : null}
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
