"use client";

/**
 * The Living Constellation: the repository's files as stars, clustered by directory and colored
 * by layer, forming while discovery finds them.
 *
 * The 3D view (WebGL2) is loaded in the browser only (`next/dynamic` with `ssr: false`: three.js
 * never runs on the server). Without WebGL2 the same layout is drawn as a static SVG scatter;
 * with reduced motion, or on a low-power device (fewer than 4 cores, Save-Data, or WebGL drawn
 * by a software rasterizer instead of a GPU), the 3D view is drawn still: no fading in, glow or
 * drift. Nothing is drawn while the view is off screen or the page hidden.
 *
 * Hovering a cluster names its directory, file count and top language; clicking a star opens a
 * preview of the file, with a link into the code browser. Color is never the only signal: the
 * legend has the layers' glyphs, the figure has a text summary, and every directory is also
 * listed in a table.
 */
import { cn } from "cn";
import { Pause, Play, X } from "lucide-react";
import { useReducedMotion } from "motion/react";
import type { Route } from "next";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useQueryStates } from "nuqs";
import {
  createContext,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { LAYERS } from "@/components/primitives/layer-badge";
import { useTrace } from "@/components/trace/use-trace";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { pathFiles, pathTitle } from "@/lib/api/trace";
import {
  clusters,
  codeSection,
  dirLabel,
  fileName,
  formatBytes,
  isLowPower,
  isSoftwareRenderer,
  traceStars,
  type Layer,
  type MotionMode,
  type Sky,
} from "@/lib/constellation";
import { repoPath } from "@/lib/routes";
import { overviewTraceParsers, pathIndex, traceHref } from "@/lib/trace/url-state";

import { useSkyColors, type Hit } from "./constellation-scene";
import { ConstellationSvg } from "./constellation-svg";
import { useConstellation } from "./use-constellation";

const ConstellationCanvas = dynamic(() => import("./constellation-canvas"), {
  ssr: false,
  loading: () => <div className="size-full" />,
});

export type Renderer = "auto" | "webgl" | "svg";

/** Which renderer constellations use when they don't say: "auto" in the app; Storybook (whose
 * test browser may lack a GPU) sets "svg", so stories and their tests draw the fallback unless
 * one asks for WebGL. */
export const ConstellationRenderer = createContext<Renderer>("auto");

const LEGEND: { layer: Layer; label: string; glyph: string }[] = [
  ...(["ui", "api", "svc", "db"] as const).map((layer) => ({
    layer,
    label: LAYERS[layer].label,
    glyph: LAYERS[layer].glyph,
  })),
  { layer: "other", label: "Other", glyph: "·" },
];
const SWATCH: Record<Layer, string> = {
  ui: "bg-layer-ui",
  api: "bg-layer-api",
  svc: "bg-layer-svc",
  db: "bg-layer-db",
  other: "bg-fg-faint",
};
const ROLE_LABELS: Record<string, string> = {
  ui: "UI",
  api: "API",
  service: "Service",
  db: "Data",
  test: "Test",
  config: "Config",
  docs: "Docs",
  other: "Other",
};
const LISTED_DIRS = 100;

interface WebGLSupport {
  webgl2: boolean;
  /** Drawn by a software rasterizer, not a GPU. */
  software: boolean;
}
let support: WebGLSupport | null = null;
/** Whether this browser can draw WebGL2, and with a GPU (asked once). */
export function webGLSupport(): WebGLSupport {
  if (support === null) {
    try {
      const probe = document.createElement("canvas").getContext("webgl2");
      // Firefox names the renderer in RENDERER (its debug extension is deprecated); Chrome and
      // Safari answer a generic "WebKit WebGL" there and the real name through the extension.
      let name: unknown = probe?.getParameter(probe.RENDERER);
      if (probe && typeof name === "string" && /^webkit webgl$/i.test(name)) {
        const debug = probe.getExtension("WEBGL_debug_renderer_info");
        if (debug) name = probe.getParameter(debug.UNMASKED_RENDERER_WEBGL);
      }
      support = {
        webgl2: probe !== null,
        software: isSoftwareRenderer(typeof name === "string" ? name : null),
      };
      // Give the probe's context back now: browsers allow only a few at a time.
      probe?.getExtension("WEBGL_lose_context")?.loseContext();
    } catch {
      support = { webgl2: false, software: false };
    }
  }
  return support;
}

const noSubscribe = () => () => undefined;

/** Whether the element is on screen and the page visible (no frames are drawn otherwise). */
function useActive(element: React.RefObject<HTMLElement | null>): boolean {
  const [onScreen, setOnScreen] = useState(true);
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const target = element.current;
    if (!target || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => {
      setOnScreen(entry?.isIntersecting ?? true);
    });
    observer.observe(target);
    return () => {
      observer.disconnect();
    };
  }, [element]);
  useEffect(() => {
    const update = () => {
      setVisible(document.visibilityState !== "hidden");
    };
    update();
    document.addEventListener("visibilitychange", update);
    return () => {
      document.removeEventListener("visibilitychange", update);
    };
  }, []);
  return onScreen && visible;
}

function summary(sky: Sky, dirsWithFiles: number): string {
  const files = sky.total ?? sky.points.length;
  const shown =
    sky.status === "streaming" && sky.total !== null
      ? `${sky.points.length.toLocaleString("en")} of ${files.toLocaleString("en")} files found so far`
      : `${files.toLocaleString("en")} files`;
  const layers = LEGEND.map(({ layer, label }) => {
    const count = sky.points.filter((p) => p.layer === layer).length;
    return count ? `${label} ${count.toLocaleString("en")}` : null;
  })
    .filter(Boolean)
    .join(", ");
  return `Constellation of the repository: ${shown} in ${dirsWithFiles.toLocaleString("en")} directories${layers ? ` (${layers})` : ""}.`;
}

interface ViewProps {
  sky: Sky;
  org: string;
  repo: string;
  /** The run is live: the view may turn slowly. */
  live?: boolean;
  renderer?: Renderer;
  /** Called for every frame the 3D view draws (ms since the last), for the FPS meter. */
  onFrame?: (ms: number) => void;
  /** A trace path to draw: its stars lit and joined by its code edges (3.15). */
  trace?: ShownTrace | null;
  /** Drawn low and wide, under a page's other content (the ready overview's). */
  strip?: boolean | undefined;
  className?: string;
}

/** A trace path on the constellation: what it's called, its files in order, and where it is. */
export interface ShownTrace {
  title: string;
  files: readonly string[];
  href: Route;
  /** Take it off the picture. */
  onClear?: () => void;
}

const NO_STARS: readonly number[] = [];

/** The trace drawn, in words and as a list of its files (what the picture shows, for everyone). */
function TraceCaption({ trace, files }: { trace: ShownTrace; files: readonly string[] }) {
  return (
    <section
      aria-label="Trace on the constellation"
      className="flex flex-col gap-1.5 rounded-md border border-signal/30 bg-signal/6 px-3 py-2 text-xs"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <p className="text-fg">
          <span aria-hidden className="text-signal">
            ●{" "}
          </span>
          Trace <span className="font-mono">{trace.title}</span>, across {files.length}{" "}
          {files.length === 1 ? "file" : "files"}
        </p>
        <Link href={trace.href} className="underline underline-offset-4">
          Open the trace
        </Link>
        {trace.onClear && (
          <Button variant="ghost" size="xs" onClick={trace.onClear}>
            Clear
          </Button>
        )}
      </div>
      <ol aria-label="Its files, in order" className="flex flex-wrap items-center gap-1">
        {files.map((file, i) => (
          <li key={file} className="flex items-center gap-1 font-mono text-fg-muted">
            {i > 0 && <span aria-hidden>→</span>}
            {fileName(file)}
          </li>
        ))}
      </ol>
    </section>
  );
}

/** The constellation of `sky` (no data fetching: stories use this directly). */
export function ConstellationView({
  sky,
  org,
  repo,
  live = false,
  renderer: asked,
  onFrame,
  trace = null,
  strip = false,
  className,
}: ViewProps) {
  const preferred = useContext(ConstellationRenderer);
  const renderer = asked ?? preferred;
  const box = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion() ?? false;
  const client = useSyncExternalStore(
    noSubscribe,
    () => true,
    () => false,
  );
  // A forced renderer (stories) keeps its motion; on its own, a software rasterizer draws still.
  const probed = client && renderer === "auto" ? webGLSupport() : null;
  const lowPower = client && (isLowPower(navigator) || probed?.software === true);
  const webgl = client && renderer !== "svg" && (renderer === "webgl" || probed?.webgl2 === true);
  const motion: MotionMode = reduced || lowPower ? "static" : "live";
  const colors = useSkyColors();
  const active = useActive(box);
  const [paused, setPaused] = useState(false);
  const [hover, setHover] = useState<Hit | null>(null);
  // With the box's size when picked, to keep the card inside it.
  const [selected, setSelected] = useState<(Hit & { width: number; height: number }) | null>(null);
  const [listOpen, setListOpen] = useState(false);
  const titleId = useId();
  const groups = useMemo(() => clusters(sky), [sky]);
  // O(points): once per sky, not on every hover.
  const label = useMemo(() => summary(sky, groups.size), [sky, groups.size]);
  const drift = live && motion === "live" && !paused;
  const traceFiles = trace?.files;
  const stars = useMemo(
    () => (traceFiles ? traceStars(sky, traceFiles) : NO_STARS),
    [sky, traceFiles],
  );
  const shownFiles = useMemo(
    () => stars.flatMap((i) => sky.points[i]?.path ?? []),
    [stars, sky.points],
  );

  const hovered = hover ? sky.points[hover.index] : undefined;
  const hoveredCluster = hovered ? groups.get(hovered.dir_cluster) : undefined;
  const picked = selected ? sky.points[selected.index] : undefined;
  const pickedDir = picked ? sky.dirs[picked.dir_cluster] : undefined;

  useEffect(() => {
    if (!picked) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
    };
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("keydown", close);
    };
  }, [picked]);

  const empty =
    sky.points.length === 0
      ? sky.status === "stopped"
        ? "This run ended before discovery found its files."
        : "Stars appear as discovery finds the files."
      : null;

  const scene = {
    sky,
    motion,
    drift,
    active,
    highlight: hovered?.dir_cluster ?? null,
    trace: stars,
    label,
    // A new hover only when it's another star: pointer moves over the same one change nothing.
    onHover: (hit: Hit | null) => {
      setHover((current) => (current?.index === hit?.index ? current : hit));
    },
    onSelect: (hit: Hit | null) => {
      const element = box.current;
      setSelected(
        hit && element
          ? { ...hit, width: element.clientWidth, height: element.clientHeight }
          : null,
      );
    },
    ...(onFrame ? { onFrame } : {}),
  };

  return (
    <figure aria-labelledby={titleId} className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <figcaption id={titleId} className="text-sm font-medium">
          Constellation
          <span className="ml-2 text-xs font-normal text-fg-muted">
            {sky.status === "streaming" ? "forming" : "files by directory"}
          </span>
        </figcaption>
        <div className="flex items-center gap-1">
          {live && motion === "live" && webgl && (
            <Button
              variant="ghost"
              size="sm"
              aria-pressed={paused}
              onClick={() => {
                setPaused((value) => !value);
              }}
            >
              {paused ? (
                <Play aria-hidden data-icon="inline-start" />
              ) : (
                <Pause aria-hidden data-icon="inline-start" />
              )}
              {paused ? "Resume motion" : "Pause motion"}
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={listOpen}
            onClick={() => {
              setListOpen((value) => !value);
            }}
          >
            {listOpen ? "Hide directory list" : "Show as a list"}
          </Button>
        </div>
      </div>
      <div
        ref={box}
        className={cn(
          "relative overflow-hidden rounded-lg border border-line bg-bg",
          strip ? "h-48 md:h-56" : "h-80 md:h-96",
        )}
      >
        {client && colors ? (
          webgl ? (
            <ConstellationCanvas {...scene} colors={colors} />
          ) : (
            <ConstellationSvg {...scene} colors={colors} />
          )
        ) : (
          <div className="size-full" />
        )}
        {empty && (
          <p className="pointer-events-none absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-fg-muted">
            {empty}
          </p>
        )}
        {hover && hoveredCluster && !selected && (
          <div
            role="tooltip"
            className="pointer-events-none absolute z-10 max-w-64 -translate-x-1/2 -translate-y-full rounded-md border border-line bg-surface-2 px-2.5 py-1.5 text-xs shadow-(--elevation-2)"
            style={{ left: hover.x, top: hover.y - 10 }}
          >
            <p className="truncate font-mono text-fg">{dirLabel(hoveredCluster.dir)}</p>
            <p className="text-fg-muted">
              {hoveredCluster.files.toLocaleString("en")}{" "}
              {hoveredCluster.files === 1 ? "file" : "files"}
              {hoveredCluster.topLanguage ? ` · mostly ${hoveredCluster.topLanguage}` : ""}
            </p>
          </div>
        )}
        {selected && picked && (
          <section
            aria-label={`File preview: ${picked.path}`}
            className="absolute z-20 flex w-72 flex-col gap-2 rounded-lg border border-line bg-surface-2 p-3 text-sm shadow-(--elevation-2)"
            style={{
              left: Math.max(8, Math.min(selected.x + 12, selected.width - 296)),
              top: Math.max(8, Math.min(selected.y - 20, selected.height - 180)),
            }}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <h3 className="truncate font-mono text-sm font-medium">{fileName(picked.path)}</h3>
                <p className="truncate font-mono text-xs text-fg-muted">
                  {pickedDir ? dirLabel(pickedDir) : ""}
                </p>
              </div>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Close the preview"
                onClick={() => {
                  setSelected(null);
                }}
              >
                <X aria-hidden />
              </Button>
            </div>
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
              <dt className="text-fg-muted">Layer</dt>
              <dd>
                {picked.layer === "other"
                  ? (ROLE_LABELS[picked.role] ?? picked.role)
                  : `${LAYERS[picked.layer].glyph} ${LAYERS[picked.layer].label}`}
              </dd>
              <dt className="text-fg-muted">Language</dt>
              <dd>{picked.language ?? "–"}</dd>
              <dt className="text-fg-muted">Size</dt>
              <dd className="tabular-nums">{formatBytes(picked.size)}</dd>
              <dt className="text-fg-muted">Lines</dt>
              <dd className="tabular-nums">
                {picked.loc === null ? "–" : picked.loc.toLocaleString("en")}
              </dd>
            </dl>
            <Link
              href={repoPath(org, repo, codeSection(picked.path))}
              className="w-fit text-sm underline underline-offset-4"
            >
              Open in code
            </Link>
          </section>
        )}
      </div>
      <ul aria-label="Layers" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted">
        {LEGEND.map(({ layer, label: name, glyph }) => (
          <li key={layer} className="flex items-center gap-1.5">
            <span aria-hidden className={cn("size-2 rounded-full", SWATCH[layer])} />
            <span aria-hidden>{glyph}</span>
            {name}
          </li>
        ))}
      </ul>
      {trace && shownFiles.length > 0 && <TraceCaption trace={trace} files={shownFiles} />}
      {listOpen && <DirectoryTable sky={sky} groups={groups} org={org} repo={repo} />}
    </figure>
  );
}

function DirectoryTable({
  sky,
  groups,
  org,
  repo,
}: {
  sky: Sky;
  groups: ReturnType<typeof clusters>;
  org: string;
  repo: string;
}) {
  const rows = [...groups.values()].sort(
    (a, b) => b.files - a.files || (a.dir.path < b.dir.path ? -1 : 1),
  );
  const shown = rows.slice(0, LISTED_DIRS);
  return (
    <div className="flex flex-col gap-1">
      <Table aria-label="Directories in the constellation">
        <TableHeader>
          <TableRow>
            <TableHead>Directory</TableHead>
            <TableHead className="text-right">Files</TableHead>
            <TableHead>Top language</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {shown.map(({ dir, files, topLanguage }) => (
            <TableRow key={dir.id}>
              <TableCell className="font-mono text-xs">
                {dir.path === "" ? (
                  dirLabel(dir)
                ) : (
                  <Link
                    href={repoPath(org, repo, codeSection(dir.path))}
                    className="underline-offset-4 hover:underline"
                  >
                    {dirLabel(dir)}
                  </Link>
                )}
              </TableCell>
              <TableCell className="text-right tabular-nums">
                {files.toLocaleString("en")}
              </TableCell>
              <TableCell className="text-fg-muted">{topLanguage ?? "–"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {rows.length > shown.length && (
        <p className="text-xs text-fg-muted">
          The {LISTED_DIRS} largest of {rows.length.toLocaleString("en")} directories (
          {sky.points.length.toLocaleString("en")} files).
        </p>
      )}
    </div>
  );
}

/** The trace the overview's URL asks for (`?trace=<entry>&path=<n>`, from the trace page), as
 * its constellation draws it; null while there's none, or it isn't read yet. */
function useShownTrace(org: string, repo: string): ShownTrace | null {
  const [{ trace: entry, path }, setQuery] = useQueryStates(overviewTraceParsers, {
    history: "replace",
    scroll: false,
  });
  const trace = useTrace(entry);
  const paths = trace.data?.paths ?? [];
  const shown = paths[pathIndex(path, paths.length)];
  if (!entry || !shown) return null;
  return {
    title: pathTitle(shown),
    files: pathFiles(shown),
    href: traceHref(org, repo, { from: entry, path }),
    onClear: () => void setQuery({ trace: null, path: null }),
  };
}

/**
 * The constellation of an index run (`job`, forming while discovery runs) or, without one, of
 * the last index (with the trace the URL asks for, if any).
 */
export function Constellation({
  org,
  repo,
  job,
  live = false,
  strip = false,
  className,
}: {
  org: string;
  repo: string;
  job: string | null;
  live?: boolean;
  strip?: boolean;
  className?: string;
}) {
  const query = useConstellation(org, repo, job);
  const trace = useShownTrace(org, repo);
  if (query.isPending) {
    return (
      <div role="status" aria-label="Loading the constellation" className={className}>
        <Skeleton
          className={
            strip
              ? "h-[calc(12rem+2.25rem)] md:h-[calc(14rem+2.25rem)]"
              : "h-[calc(20rem+2.25rem)] md:h-[calc(24rem+2.25rem)]"
          }
        />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className={cn("flex items-center gap-3 rounded-lg border border-line p-4", className)}>
        <p className="text-sm text-fg-muted">Couldn&apos;t load the constellation.</p>
        <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  return (
    <ConstellationView
      sky={query.data}
      org={org}
      repo={repo}
      live={live}
      trace={trace}
      strip={strip}
      {...(className ? { className } : {})}
    />
  );
}
