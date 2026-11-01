"use client";

/**
 * `/[org]/[repo]/map` (6.9): the Codebase Galaxy, full screen. Every file a star, by meaning
 * (the default: files about the same thing near each other) or by folder; its clusters named,
 * a click on a name flying there; a search lighting the stars it finds; a click on a star its
 * file's card; `?trace=` (and `path`) a trace's stars joined in order.
 *
 * Until the semantic layout is made (or without summaries), the folder one is shown, saying why.
 * Without WebGL2 the same stars are drawn flat (the graph explorer's 2D canvas); with reduced
 * motion there is no drift and the camera jumps.
 */
import { Code2, Orbit, Radar, Search, X } from "lucide-react";
import { useReducedMotionConfig } from "motion/react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { parseAsStringLiteral, useQueryState, useQueryStates } from "nuqs";
import { useCallback, useMemo, useState } from "react";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { useGraphColors } from "@/components/graph/graph-colors";
import { useGraphSearch } from "@/components/graph/use-deps";
import { webGLSupport } from "@/components/indexing/constellation";
import { useSkyColors } from "@/components/indexing/constellation-scene";
import { useViewRef } from "@/components/repo/use-ref";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { useTrace } from "@/components/trace/use-trace";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { $api } from "@/lib/api/client";
import type { ShownNode } from "@/lib/api/deps";
import { ApiError } from "@/lib/api/errors";
import {
  GALAXY_LAYOUTS,
  LAYOUT_LABELS,
  starsOf,
  type Galaxy,
  type GalaxyLayout,
  type Rgb,
} from "@/lib/api/galaxy";
import { impactHref } from "@/lib/api/impact";
import { pathFiles } from "@/lib/api/trace";
import { codePath, repoPath } from "@/lib/routes";
import { overviewTraceParsers, pathIndex } from "@/lib/trace/url-state";

const GalaxyCanvas = dynamic(() => import("./galaxy-canvas"), {
  ssr: false,
  loading: () => <div className="size-full" />,
});
const FlatCanvas = dynamic(() => import("@/components/graph/graph-canvas"), {
  ssr: false,
  loading: () => <div className="size-full" />,
});

const POLL_MS = 3_000;
const GREY: Rgb = [0.6, 0.6, 0.6];

function useGalaxy(layout: GalaxyLayout) {
  const { org, repo } = useRepoSlug();
  const [ref] = useViewRef();
  return $api.useQuery(
    "get",
    "/api/v1/orgs/{org}/repos/{repo}/galaxy",
    { params: { path: { org, repo }, query: { layout, ref } } },
    {
      staleTime: 5 * 60_000,
      retry: false,
      // Being made: asked again until it is there.
      refetchInterval: (query) =>
        query.state.error instanceof ApiError && query.state.error.code === "galaxy_computing"
          ? POLL_MS
          : false,
    },
  );
}

export function GalaxyPage() {
  const { org, repo } = useRepoSlug();
  const { state, ready } = useRepoFeatures();
  if (!ready) return null;
  if (!state("code_browser").available) {
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col p-8">
        <EmptyState
          icon={<Orbit aria-hidden />}
          title="The map opens after indexing"
          description="Every file is a star once the repository's files are discovered."
          action={
            <Link href={repoPath(org, repo)} className={buttonVariants({ size: "sm" })}>
              Back to the overview
            </Link>
          }
        />
      </div>
    );
  }
  return <Map />;
}

function Map() {
  const { org, repo } = useRepoSlug();
  const [layout, setLayout] = useQueryState(
    "layout",
    parseAsStringLiteral(GALAXY_LAYOUTS)
      .withDefault("semantic")
      .withOptions({ history: "replace" }),
  );
  const [{ trace: entry, path: pathNumber }] = useQueryStates(overviewTraceParsers);
  const semantic = useGalaxy("semantic");
  const structural = useGalaxy("structural");
  const sky = useSkyColors();
  const graphColors = useGraphColors();
  const still = useReducedMotionConfig() ?? false;
  const [webgl] = useState(() => typeof document === "undefined" || webGLSupport().webgl2);
  const [selected, setSelected] = useState<number | null>(null);
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const [drift, setDrift] = useState(false);
  const [flight, setFlight] = useState<{ cluster: number; nonce: number } | null>(null);

  const semanticError = semantic.error instanceof ApiError ? semantic.error.code : null;
  const shownLayout: GalaxyLayout =
    layout === "semantic" && semantic.data ? "semantic" : "structural";
  const galaxy: Galaxy | undefined = shownLayout === "semantic" ? semantic.data : structural.data;

  const colorOf = useCallback(
    (layer: string): Rgb => {
      const layers: Partial<Record<string, Rgb>> = sky?.layers ?? {};
      return layers[layer] ?? GREY;
    },
    [sky],
  );

  // Search: the stars whose path has the words, and those the search's hits are in.
  const search = useGraphSearch(query, query.trim().length > 0);
  const lit = useMemo(() => {
    if (!galaxy || !query.trim()) return null;
    const wanted = query.trim().toLowerCase();
    const found = new Set<number>();
    galaxy.stars.forEach((star, i) => {
      if (star.path.toLowerCase().includes(wanted)) found.add(i);
    });
    const hits = Object.values(search.data?.lanes ?? {}).flatMap((lane) => lane.map((h) => h.path));
    for (const i of starsOf(galaxy.stars, hits)) found.add(i);
    return found;
  }, [galaxy, query, search.data]);

  // A trace's stars, in its order.
  const traced = useTrace(entry);
  const tracePath = traced.data?.paths[pathIndex(pathNumber, traced.data.paths.length)];
  const trace = useMemo(
    () => (galaxy && tracePath ? starsOf(galaxy.stars, pathFiles(tracePath)) : []),
    [galaxy, tracePath],
  );

  const flat = useMemo(() => {
    if (!galaxy || webgl || !graphColors) return null;
    const nodes: ShownNode[] = galaxy.stars.map((star) => ({
      id: star.path,
      label: star.path.split("/").at(-1) ?? star.path,
      layer: star.layer === "svc" ? "service" : star.layer === "api" ? "route" : star.layer,
      size: 1,
      cluster: String(star.cluster),
      x: star.x,
      y: star.y,
      level: "file",
    }));
    return { nodes, sizes: new globalThis.Map(nodes.map((n) => [n.id, 3])) };
  }, [galaxy, webgl, graphColors]);

  const star = galaxy && selected !== null ? galaxy.stars[selected] : undefined;
  const cluster = star ? galaxy?.clusters.find((c) => c.id === star.cluster) : undefined;
  const failed = shownLayout === "structural" ? structural.isError : false;

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2">
        <h1 className="flex items-center gap-1.5 text-sm font-medium">
          <Orbit aria-hidden className="size-4 text-fg-muted" />
          Codebase galaxy
        </h1>
        <ToggleGroup
          aria-label="Layout"
          value={[layout]}
          onValueChange={(value: string[]) => {
            const next = value[0];
            if (next === "semantic" || next === "structural") {
              setSelected(null);
              void setLayout(next);
            }
          }}
        >
          {GALAXY_LAYOUTS.map((l) => (
            <ToggleGroupItem key={l} value={l} size="sm">
              {LAYOUT_LABELS[l]}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <form
          role="search"
          aria-label="Search the galaxy"
          className="flex items-center gap-1"
          onSubmit={(event) => {
            event.preventDefault();
            setQuery(typed.trim());
          }}
        >
          <Input
            aria-label="Find stars"
            placeholder="A file, or what it does"
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
            }}
            className="h-8 w-60"
          />
          <Button type="submit" size="icon" variant="ghost" aria-label="Find">
            <Search aria-hidden />
          </Button>
          {query ? (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              aria-label="Clear the search"
              onClick={() => {
                setTyped("");
                setQuery("");
              }}
            >
              <X aria-hidden />
            </Button>
          ) : null}
        </form>
        {lit ? (
          <p className="text-xs text-fg-muted" data-testid="galaxy-found" aria-live="polite">
            {search.isFetching
              ? "Searching…"
              : `${String(lit.size)} ${lit.size === 1 ? "star" : "stars"} lit`}
          </p>
        ) : null}
        {!still && webgl ? (
          <span className="ml-auto flex items-center gap-1.5 text-xs text-fg-muted">
            <Switch checked={drift} onCheckedChange={setDrift} aria-label="Drift" />
            <span aria-hidden>Drift</span>
          </span>
        ) : null}
      </div>
      {layout === "semantic" && shownLayout === "structural" && semanticError ? (
        <p
          className="border-b border-line px-4 py-1.5 text-xs text-fg-muted"
          data-testid="galaxy-note"
        >
          {semanticError === "galaxy_computing"
            ? "Working out the layout by meaning; shown by folder meanwhile."
            : "By folder: placing files by meaning needs this commit's summaries."}
        </p>
      ) : null}
      {!webgl ? (
        <p className="border-b border-line px-4 py-1.5 text-xs text-fg-muted">
          This browser can&apos;t draw WebGL2: the galaxy is drawn flat.
        </p>
      ) : null}
      <div className="flex min-h-0 flex-1">
        <div
          className="relative h-[calc(100dvh-11rem)] min-h-[28rem] flex-1 overflow-hidden bg-bg"
          data-testid="galaxy-canvas"
          data-layout={shownLayout}
          data-stars={galaxy?.stars.length ?? 0}
          data-trace={trace.length}
        >
          {failed ? (
            <div className="p-6">
              <ErrorState
                title="Couldn't load the galaxy"
                onRetry={() => void structural.refetch()}
              />
            </div>
          ) : !galaxy || !sky || !graphColors ? (
            <div role="status" aria-label="Loading the galaxy" className="p-6">
              <Skeleton className="h-96" />
            </div>
          ) : webgl ? (
            <GalaxyCanvas
              stars={galaxy.stars}
              clusters={galaxy.clusters}
              colorOf={colorOf}
              lit={lit}
              trace={trace}
              selected={selected}
              flight={flight}
              drift={drift}
              still={still}
              traceColor={graphColors.lit}
              label={`${String(galaxy.stars.length)} files in ${String(galaxy.clusters.length)} clusters, ${LAYOUT_LABELS[shownLayout].toLowerCase()}`}
              onSelect={setSelected}
              onCluster={(id) => {
                setFlight((held) => ({ cluster: id, nonce: (held?.nonce ?? 0) + 1 }));
              }}
            />
          ) : flat ? (
            <FlatCanvas
              view={{
                nodes: flat.nodes,
                edges: [],
                sizes: flat.sizes,
                colors: graphColors,
                focus: null,
                found: new Set(lit ? [...lit].map((i) => galaxy.stars[i]?.path ?? "") : []),
                path: null,
                cycles: new Set(),
                selected: star?.path ?? null,
              }}
              label={`${String(galaxy.stars.length)} files, drawn flat`}
              still
              onSelectNode={(id) => {
                setSelected(id === null ? null : galaxy.stars.findIndex((s) => s.path === id));
              }}
            />
          ) : null}
        </div>
        <aside
          aria-label="Clusters"
          className="flex w-72 shrink-0 flex-col gap-3 overflow-y-auto border-l border-line p-4"
        >
          {star ? (
            <section
              aria-label="File"
              data-testid="galaxy-file"
              className="flex flex-col gap-2 rounded-md border border-line p-3"
            >
              <div className="flex items-start gap-2">
                <h2 className="min-w-0 flex-1 font-mono text-xs break-all">{star.path}</h2>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label="Close the file"
                  onClick={() => {
                    setSelected(null);
                  }}
                >
                  <X aria-hidden />
                </Button>
              </div>
              <p className="text-xs text-fg-muted">
                {[star.language, star.layer, cluster?.label].filter(Boolean).join(" · ")}
              </p>
              <div className="flex flex-wrap gap-1.5">
                <Link
                  href={codePath(org, repo, star.path)}
                  className={buttonVariants({ size: "sm", variant: "outline" })}
                >
                  <Code2 aria-hidden />
                  Open the code
                </Link>
                <Link
                  href={impactHref(org, repo, { files: [star.path] })}
                  className={buttonVariants({ size: "sm", variant: "outline" })}
                >
                  <Radar aria-hidden />
                  Impact
                </Link>
              </div>
            </section>
          ) : null}
          <h2 className="text-xs font-medium text-fg-muted">Clusters</h2>
          <ul className="flex flex-col gap-0.5" data-testid="galaxy-clusters">
            {(galaxy?.clusters ?? []).map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  className="flex w-full items-baseline gap-2 rounded-sm px-2 py-1 text-left text-xs hover:bg-surface-2"
                  onClick={() => {
                    setFlight((held) => ({ cluster: c.id, nonce: (held?.nonce ?? 0) + 1 }));
                  }}
                >
                  <span className="min-w-0 flex-1 truncate">{c.label}</span>
                  <span className="text-fg-muted tabular-nums">{c.files}</span>
                </button>
              </li>
            ))}
          </ul>
          <p className="text-xs text-fg-faint">
            Drag to turn, scroll to zoom; a cluster&apos;s name flies there; a star opens its file.
          </p>
        </aside>
      </div>
    </div>
  );
}
