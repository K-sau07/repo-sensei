"use client";

/**
 * `/[org]/[repo]/diagrams/[id]` (6.6): a diagram on its canvas. The header says how much of it
 * the code graph proves ("84% statically verified"), and can hide what it doesn't; when a file it
 * cites has changed since it was drawn it says so, with "Regenerate" (drawn again from the same
 * question or trace; what the new one added is ringed, what it dropped listed). An outline says
 * it all as text. A node opens its code, a question about it and its trace beside the canvas.
 */
import { History, List, RefreshCw, Share2, ShieldCheck, Trash2, Workflow } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { parseAsString, parseAsStringLiteral, useQueryState } from "nuqs";
import { useState } from "react";
import { toast } from "sonner";

import { useStaleness } from "@/components/ask/use-staleness";
import { StaleBadge } from "@/components/feedback/banners";
import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  citedFiles,
  DIAGRAM_KINDS,
  KIND_LABELS,
  outline,
  redrawInput,
  shownEdges,
  specDiff,
  specOf,
  verifiedPercent,
  viewsOf,
  type Diagram,
  type DiagramKind,
  type SpecNode,
} from "@/lib/api/diagrams";
import { ApiError } from "@/lib/api/errors";
import { isStale } from "@/lib/api/refs";
import { repoPath } from "@/lib/routes";

import { DiagramCanvas } from "./diagram-canvas";
// A node's panel (its code, the code viewer with it) loads when one is picked.
const NodePanel = dynamic(() => import("./node-panel").then((m) => m.NodePanel), {
  loading: () => <p className="p-3 text-xs text-fg-faint">Loading…</p>,
});
import { useCreateDiagram, useDiagram, useDiagramActions } from "./use-diagrams";

export function DiagramPage() {
  const { diagramId } = useParams<{ diagramId: string }>();
  const diagram = useDiagram(diagramId);
  if (diagram.isPending) {
    return (
      <div className="flex flex-col gap-3 p-6" role="status" aria-label="Loading the diagram">
        <Skeleton className="h-8 w-80" />
        <Skeleton className="h-[28rem]" />
      </div>
    );
  }
  if (diagram.isError) {
    const gone = diagram.error instanceof ApiError && diagram.error.status === 404;
    return (
      <div className="p-8">
        <ErrorState
          title={gone ? "No such diagram" : "Couldn't load the diagram"}
          {...(gone ? { description: "It may have been deleted." } : {})}
          {...(gone ? {} : { onRetry: () => void diagram.refetch() })}
        />
      </div>
    );
  }
  return <Loaded diagram={diagram.data} />;
}

function Loaded({ diagram }: { diagram: Diagram }) {
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  const spec = specOf(diagram);
  const [hide, setHide] = useState(false);
  const [showOutline, setShowOutline] = useState(false);
  const [selected, setSelected] = useState<SpecNode | null>(null);
  const [since] = useQueryState("since", parseAsString);
  // A flow in any of its three views (6.6.2): drawn as its kind until another is picked. An ER
  // diagram is drawn only as one.
  const [picked, setView] = useQueryState("view", parseAsStringLiteral(DIAGRAM_KINDS));
  const views = viewsOf(diagram.kind);
  const view = picked && views.includes(picked) ? picked : diagram.kind;
  const before = useDiagram(since ?? "", since !== null);
  const diff = since && before.data ? specDiff(specOf(before.data), spec) : null;
  const cited = citedFiles(spec, diagram.commit_sha);
  const { checked } = useStaleness(cited);
  const changed = cited.filter((c) => isStale(checked.get(`${c.sha.toLowerCase()}:${c.path}`)));
  const create = useCreateDiagram();
  const edges = shownEdges(spec, hide);
  const verified = spec.edges.filter((e) => e.verified).length;

  // Drawing again can find nothing now, or be rate limited: said, not swallowed.
  const failed = (caught: unknown) => {
    toast.error(caught instanceof ApiError ? caught.message : "The diagram couldn't be drawn.");
  };
  const redraw = async () => {
    const input = redrawInput(diagram);
    if (!input) return;
    try {
      const made = await create.draw(input);
      router.push(
        `${repoPath(org, repo, `diagrams/${made.id}`)}?since=${diagram.id}` as Parameters<
          typeof router.push
        >[0],
      );
    } catch (caught) {
      failed(caught);
    }
  };
  const expand = async (node: SpecNode) => {
    try {
      const made = await create.draw({
        from_trace: { from: node.id, direction: "down", kind: "auto", path: 1 },
        kind: diagram.kind,
      });
      router.push(repoPath(org, repo, `diagrams/${made.id}`));
    } catch (caught) {
      failed(caught);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="diagram-page">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line px-6 py-3">
        <Link
          href={repoPath(org, repo, "diagrams")}
          className="text-sm text-fg-muted hover:text-fg"
        >
          Diagrams
        </Link>
        <span aria-hidden className="text-fg-faint">
          /
        </span>
        <Title diagram={diagram} />
        {views.length > 1 ? (
          <ToggleGroup
            aria-label="View as"
            value={[view]}
            onValueChange={(value: string[]) => {
              const next = views.find((kind) => kind === value[0]);
              if (next) void setView(next === diagram.kind ? null : next);
            }}
          >
            {views.map((kind) => (
              <ToggleGroupItem key={kind} value={kind} size="sm">
                {KIND_LABELS[kind]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        ) : (
          <span className="rounded-full border border-line px-2 py-0.5 text-xs text-fg-muted">
            {KIND_LABELS[diagram.kind]}
          </span>
        )}
        <Tooltip>
          <TooltipTrigger
            render={
              <span
                className="inline-flex items-center gap-1 rounded-full border border-ok/30 px-2 py-0.5 text-xs text-ok"
                data-testid="verified-ratio"
              />
            }
          >
            <ShieldCheck aria-hidden className="size-3.5" />
            {verifiedPercent(diagram.verified_ratio)} statically verified
          </TooltipTrigger>
          <TooltipContent>
            {verified} of {spec.edges.length} edges are in the code graph; the rest are inferred
            (dashed), each with why.
          </TooltipContent>
        </Tooltip>
        {changed.length > 0 ? (
          <span className="flex items-center gap-2" data-testid="diagram-stale">
            <StaleBadge>
              {changed.length} cited file{changed.length === 1 ? "" : "s"} changed since
            </StaleBadge>
            {redrawInput(diagram) ? (
              <Button
                size="sm"
                variant="outline"
                disabled={create.isPending}
                onClick={() => void redraw()}
              >
                <RefreshCw aria-hidden />
                {create.isPending ? "Drawing…" : "Regenerate"}
              </Button>
            ) : null}
          </span>
        ) : null}
        <div className="ml-auto flex items-center gap-3">
          <span className="flex items-center gap-1.5 text-xs text-fg-muted">
            <Switch
              id="hide-unverified"
              checked={hide}
              onCheckedChange={setHide}
              aria-label="Hide unverified edges"
            />
            <span aria-hidden>Hide unverified</span>
          </span>
          <Button
            size="sm"
            variant={showOutline ? "secondary" : "ghost"}
            aria-pressed={showOutline}
            onClick={() => {
              setShowOutline((was) => !was);
            }}
          >
            <List aria-hidden />
            Outline
          </Button>
          <Actions diagram={diagram} />
        </div>
      </header>
      {spec.summary ? (
        <p
          className="border-b border-line px-6 py-2 text-sm text-fg-muted"
          data-testid="diagram-summary"
        >
          {spec.summary}
        </p>
      ) : null}
      {diff ? <Changes added={diff.added.size} removed={diff.removed} /> : null}
      <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[minmax(0,1fr)_24rem]">
        <div className="min-h-[28rem]">
          {spec.nodes.length === 0 ? (
            <EmptyState
              icon={<Workflow aria-hidden />}
              title="Nothing to draw"
              description="The graph had nothing for this diagram."
            />
          ) : (
            <DiagramCanvas
              spec={spec}
              edges={edges}
              mermaid={diagram.mermaid}
              title={diagram.title}
              selected={selected?.id ?? null}
              onSelect={setSelected}
              added={diff?.added}
              view={view}
            />
          )}
        </div>
        <div className="hidden min-h-0 md:block">
          {selected ? (
            <NodePanel
              node={selected}
              sha={diagram.commit_sha}
              onClose={() => {
                setSelected(null);
              }}
              onExpand={(node) => void expand(node)}
              expanding={create.isPending}
            />
          ) : showOutline ? (
            <Outline steps={outline({ ...spec, edges })} />
          ) : (
            <Outline steps={outline({ ...spec, edges })} compact kind={diagram.kind} />
          )}
        </div>
      </div>
    </div>
  );
}

function Title({ diagram }: { diagram: Diagram }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(diagram.title);
  const actions = useDiagramActions(diagram.id);
  if (!editing) {
    return (
      <button
        type="button"
        className="max-w-[32rem] truncate text-left text-base font-semibold hover:underline"
        title="Rename"
        onClick={() => {
          setEditing(true);
        }}
      >
        <h1 className="inline">{diagram.title}</h1>
      </button>
    );
  }
  return (
    <form
      className="flex items-center gap-1.5"
      onSubmit={(event) => {
        event.preventDefault();
        if (title.trim()) {
          void actions.rename(title.trim()).then(() => {
            setEditing(false);
          });
        }
      }}
    >
      <Input
        aria-label="Title"
        value={title}
        maxLength={200}
        onChange={(event) => {
          setTitle(event.target.value);
        }}
        className="h-8 w-80"
      />
      <Button type="submit" size="sm" disabled={actions.busy}>
        Save
      </Button>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        onClick={() => {
          setEditing(false);
          setTitle(diagram.title);
        }}
      >
        Cancel
      </Button>
    </form>
  );
}

function Actions({ diagram }: { diagram: Diagram }) {
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  const actions = useDiagramActions(diagram.id);
  const [link, setLink] = useState<string | null>(null);
  return (
    <span className="flex items-center gap-1">
      {link ? (
        <Input
          readOnly
          aria-label="Share link"
          value={link}
          className="h-8 w-56 font-mono text-xs"
          onFocus={(event) => {
            event.currentTarget.select();
          }}
        />
      ) : null}
      <Button
        size="sm"
        variant="ghost"
        disabled={actions.busy}
        onClick={() =>
          void actions.share().then((shared) => {
            const url = `${window.location.origin}${shared.path}`;
            setLink(url);
            void navigator.clipboard.writeText(url).catch(() => undefined);
          })
        }
      >
        <Share2 aria-hidden />
        Share
      </Button>
      <Button
        size="icon-sm"
        variant="ghost"
        aria-label="Delete diagram"
        disabled={actions.busy}
        onClick={() => {
          if (window.confirm(`Delete "${diagram.title}"?`)) {
            void actions.remove().then(() => {
              router.push(repoPath(org, repo, "diagrams"));
            });
          }
        }}
      >
        <Trash2 aria-hidden />
      </Button>
    </span>
  );
}

function Changes({ added, removed }: { added: number; removed: readonly SpecNode[] }) {
  return (
    <p
      role="status"
      className="flex flex-wrap items-center gap-2 border-b border-line bg-surface-2 px-6 py-1.5 text-xs text-fg-muted"
      data-testid="diagram-changes"
    >
      <History aria-hidden className="size-3.5" />
      Drawn again: {added} step{added === 1 ? "" : "s"} new (ringed)
      {removed.length > 0
        ? `; no longer in it: ${removed.map((node) => node.label).join(", ")}`
        : "; none dropped"}
      .
    </p>
  );
}

/** What the outline lists, by the diagram's kind. */
const LISTED: Record<DiagramKind, string> = {
  flowchart: "Steps",
  sequence: "Messages",
  architecture: "Connections",
  er: "Relations",
};

function Outline({
  steps,
  compact = false,
  kind = "flowchart",
}: {
  steps: string[];
  compact?: boolean;
  kind?: DiagramKind;
}) {
  return (
    <section
      aria-label="Outline"
      className="flex h-full min-h-0 flex-col border-l border-line"
      data-testid="diagram-outline"
    >
      <h2 className="border-b border-line px-3 py-2 text-xs font-medium text-fg-muted">
        {compact ? `${LISTED[kind]} (pick a box for more)` : "Outline"}
      </h2>
      <ol className="min-h-0 flex-1 list-decimal overflow-auto py-2 pr-3 pl-8 text-xs">
        {steps.map((step, i) => (
          <li key={`${String(i)}-${step}`} className="py-0.5">
            {step}
          </li>
        ))}
      </ol>
    </section>
  );
}
