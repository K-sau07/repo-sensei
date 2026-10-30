"use client";

/**
 * A repository's overview once it is indexed (4.13): what it does (the summaries stage's
 * rollups), how it is laid out (its modules as a map and a table), questions worth asking, its
 * feature maps, how well the index understood it, and the last index itself (with the
 * constellation as a strip). A missing piece says so instead of being made up.
 */
import { cn } from "cn";
import { MessageCircleQuestion } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { askHref } from "@/components/ask/ask-bar";
import { ErrorState } from "@/components/feedback/error-state";
import { Constellation } from "@/components/indexing/constellation";
import { RelativeTime } from "@/components/primitives/relative-time";
import { LanguageBar } from "@/components/repos/language-bar";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import {
  dominantLayer,
  healthTiles,
  MAP_LAYERS,
  type Health,
  type Module,
  type Overview,
} from "@/lib/api/overview";
import type { IndexJob, Repo } from "@/lib/api/types";
import { took } from "@/lib/indexing";
import { codePath, repoPath } from "@/lib/routes";

const ArchitectureMap = dynamic(
  () => import("./architecture-map").then((module) => module.ArchitectureMap),
  { ssr: false, loading: () => <Skeleton className="h-80" /> },
);

const LAYER_NAMES: Record<string, string> = {
  ui: "UI",
  api: "API",
  svc: "Service",
  db: "Data",
  other: "Other",
};

const TONE = { ok: "text-ok", warn: "text-warn", muted: "text-fg-muted" } as const;

function Section({
  id,
  title,
  aside,
  className,
  children,
}: {
  id: string;
  title: string;
  aside?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      aria-labelledby={id}
      className={cn("flex flex-col gap-3 rounded-lg border border-line p-5", className)}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={id} className="text-sm font-medium">
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function WhatItDoes({ org, repo, overview }: { org: string; repo: string; overview: Overview }) {
  const { summary } = overview;
  return (
    <Section id="overview-summary" title="What this repo does">
      {summary ? (
        <>
          <p className="text-sm leading-6 text-pretty">{summary.text}</p>
          {summary.directories.length > 0 && (
            <ul className="grid gap-2 sm:grid-cols-2">
              {summary.directories.map((d) => (
                <li key={d.path} className="flex flex-col gap-0.5 text-sm">
                  <Link
                    href={codePath(org, repo, d.path)}
                    className="w-fit font-mono text-xs underline underline-offset-4"
                  >
                    {d.path}/
                  </Link>
                  <span className="text-fg-muted">{d.summary}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <p className="text-sm text-fg-muted">
          No summary: the summaries stage didn&apos;t run at this index. Turn it on in Settings and
          re-index to have one written.
        </p>
      )}
    </Section>
  );
}

function layersOf(module: Module): string {
  return MAP_LAYERS.filter((l) => module.layers[l])
    .map((l) => `${LAYER_NAMES[l] ?? l} ${String(module.layers[l])}`)
    .join(", ");
}

function Architecture({ org, repo, overview }: { org: string; repo: string; overview: Overview }) {
  const router = useRouter();
  const { modules, links, rest } = overview.architecture;
  const open = (module: Module) => codePath(org, repo, module.path === "." ? "" : module.path);
  return (
    <Section
      id="overview-architecture"
      title="Architecture"
      aside={
        <span className="text-xs text-fg-muted">
          {modules.length} modules
          {rest > 0 ? ` · ${rest.toLocaleString("en")} files in smaller ones` : ""} · {links.length}{" "}
          links between them
        </span>
      }
    >
      {modules.length === 0 ? (
        <p className="text-sm text-fg-muted">No files at this index.</p>
      ) : (
        <>
          <ArchitectureMap
            modules={modules}
            links={links}
            onOpen={(module) => {
              router.push(open(module));
            }}
          />
          <Table aria-label="Modules">
            <TableHeader>
              <TableRow>
                <TableHead>Module</TableHead>
                <TableHead>Mostly</TableHead>
                <TableHead className="text-right">Files</TableHead>
                <TableHead>By layer</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {modules.map((module) => (
                <TableRow key={module.path}>
                  <TableCell>
                    <Link
                      href={open(module)}
                      className="font-mono text-xs underline underline-offset-4"
                    >
                      {module.path === "." ? "(top level)" : `${module.path}/`}
                    </Link>
                  </TableCell>
                  <TableCell className="text-xs">
                    {LAYER_NAMES[dominantLayer(module.layers)]}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{module.files}</TableCell>
                  <TableCell className="text-xs text-fg-muted">{layersOf(module)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </Section>
  );
}

function Questions({ org, repo, overview }: { org: string; repo: string; overview: Overview }) {
  if (overview.questions.length === 0) return null;
  return (
    <Section id="overview-questions" title="Ask about it">
      <ul className="flex flex-col gap-1.5">
        {overview.questions.map((question) => (
          <li key={question}>
            <Link
              href={askHref(org, repo, question, null)}
              className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <MessageCircleQuestion aria-hidden className="size-4 shrink-0 text-fg-muted" />
              {question}
            </Link>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function FeatureMaps({ org, repo, overview }: { org: string; repo: string; overview: Overview }) {
  if (overview.feature_maps.length === 0) return null;
  return (
    <Section id="overview-feature-maps" title="Feature maps">
      <ul className="grid gap-3 sm:grid-cols-2">
        {overview.feature_maps.map((m) => (
          <li key={m.key} className="flex flex-col gap-1.5 rounded-md border border-line p-3">
            <span className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium">{m.title}</span>
              {m.stale && (
                <Badge variant="outline" className="text-warn">
                  Out of date
                </Badge>
              )}
            </span>
            <span className="text-sm text-fg-muted">{m.summary}</span>
            <Link
              href={codePath(org, repo, m.path)}
              className="w-fit font-mono text-xs underline underline-offset-4"
            >
              {m.path}/
            </Link>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function HealthTiles({ health }: { health: Health }) {
  return (
    <Section id="overview-health" title="Index health">
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {healthTiles(health).map((tile) => (
          <div key={tile.label} className="flex flex-col gap-0.5">
            <dt className="text-xs text-fg-muted">{tile.label}</dt>
            <dd className={cn("text-xl font-semibold tabular-nums", TONE[tile.tone])}>
              {tile.value}
            </dd>
            <dd className="text-xs text-fg-muted">{tile.detail}</dd>
          </div>
        ))}
      </dl>
      {health.languages.length > 0 && (
        <p className="text-xs text-fg-muted">
          {health.languages
            .slice(0, 6)
            .map((l) => `${l.language}: symbols in ${String(l.with_symbols)} of ${String(l.files)}`)
            .join(" · ")}
        </p>
      )}
    </Section>
  );
}

function IndexSummary({
  org,
  repo,
  data,
  latest,
}: {
  org: string;
  repo: string;
  data: Repo;
  latest: IndexJob;
}) {
  return (
    <Section
      id="index-summary"
      title={`Indexed ${data.last_indexed_sha?.slice(0, 12) ?? ""}`}
      aside={
        data.last_indexed_at && (
          <span className="text-xs text-fg-muted">
            <RelativeTime date={data.last_indexed_at} /> · took {took(latest)}
          </span>
        )
      }
    >
      <dl className="grid grid-cols-3 gap-4">
        <div>
          <dt className="text-xs text-fg-muted">Files</dt>
          <dd className="text-xl font-semibold tabular-nums">
            {(data.file_count ?? 0).toLocaleString("en")}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-fg-muted">Lines</dt>
          <dd className="text-xl font-semibold tabular-nums">
            {(data.loc ?? 0).toLocaleString("en")}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-fg-muted">Languages</dt>
          <dd className="text-xl font-semibold tabular-nums">
            {Object.keys(data.languages).length}
          </dd>
        </div>
      </dl>
      <LanguageBar languages={data.languages} legend />
      <Constellation org={org} repo={repo} job={null} strip />
      <Link
        href={repoPath(org, repo, "index")}
        className="w-fit text-sm underline underline-offset-4"
      >
        Indexing details and history
      </Link>
    </Section>
  );
}

export function RepoReady({
  org,
  repo,
  data,
  latest,
}: {
  org: string;
  repo: string;
  data: Repo;
  latest: IndexJob;
}) {
  const overview = $api.useQuery("get", "/api/v1/orgs/{org}/repos/{repo}/overview", {
    params: { path: { org, repo } },
  });
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-6">
      {overview.isPending && (
        <div role="status" aria-label="Loading the overview" className="flex flex-col gap-6">
          <Skeleton className="h-32" />
          <Skeleton className="h-96" />
        </div>
      )}
      {overview.isError && (
        <ErrorState
          title="Couldn't load the overview"
          description={overview.error instanceof ApiError ? overview.error.message : undefined}
          onRetry={() => void overview.refetch()}
        />
      )}
      {overview.data && (
        <>
          <WhatItDoes org={org} repo={repo} overview={overview.data} />
          <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
            <Architecture org={org} repo={repo} overview={overview.data} />
            <div className="flex flex-col gap-6">
              <Questions org={org} repo={repo} overview={overview.data} />
            </div>
          </div>
          <FeatureMaps org={org} repo={repo} overview={overview.data} />
          <HealthTiles health={overview.data.health} />
        </>
      )}
      <IndexSummary org={org} repo={repo} data={data} latest={latest} />
    </div>
  );
}
