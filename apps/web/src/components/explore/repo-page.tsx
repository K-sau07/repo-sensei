/**
 * A published repository's public page (10.2), in three columns: what's on the page, the page
 * (what it does, how it's laid out, its feature maps, each symbol a chip that opens its lines on
 * GitHub), and how fresh it is (the commit it was verified at) with its languages and graph.
 * The floating Ask bar is over it all.
 */
import { ExternalLink } from "lucide-react";
import Link from "next/link";

import { LayerBadge } from "@/components/primitives/layer-badge";
import { buttonVariants } from "@/components/ui/button";
import {
  githubBlob,
  githubCommit,
  githubTree,
  languageShares,
  mapSymbols,
  shortSha,
  type FeatureMap,
  type MapSymbol,
  type PublicRepoPage,
} from "@/lib/api/explore";
import { moduleLabel } from "@/lib/api/overview";

import { ExploreAsk, type HumanCheck } from "./explore-ask";
import { LanguageBar } from "./gallery";

const DAY = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" });
const COUNT = new Intl.NumberFormat("en");
const LAYERS = ["ui", "api", "svc", "db"] as const;

export function RepoPage({
  page,
  maps,
  human,
}: {
  page: PublicRepoPage;
  maps: readonly FeatureMap[];
  human: HumanCheck;
}) {
  const { repo, overview } = page;
  const full = repo.full_name;
  return (
    <>
      <div className="mx-auto grid w-full max-w-7xl gap-8 px-6 pt-8 pb-64 lg:grid-cols-[13rem_minmax(0,1fr)_16rem]">
        <nav
          aria-label="On this page"
          className="hidden self-start text-sm lg:sticky lg:top-6 lg:block"
        >
          <p className="mb-2 text-xs font-medium tracking-wide text-fg-faint uppercase">
            On this page
          </p>
          <ul className="flex flex-col gap-1.5">
            <li>
              <a href="#overview" className="text-fg-muted hover:text-fg">
                What it does
              </a>
            </li>
            <li>
              <a href="#layout" className="text-fg-muted hover:text-fg">
                How it&apos;s laid out
              </a>
            </li>
            {maps.length > 0 && (
              <li>
                <a href="#maps" className="text-fg-muted hover:text-fg">
                  Feature maps
                </a>
                <ul className="mt-1.5 flex flex-col gap-1 border-l border-border pl-3">
                  {maps.map((map, i) => (
                    <li key={map.key}>
                      <a href={`#map-${i + 1}`} className="text-fg-muted hover:text-fg">
                        {map.title}
                      </a>
                    </li>
                  ))}
                </ul>
              </li>
            )}
          </ul>
        </nav>

        <article className="flex min-w-0 flex-col gap-10">
          <header className="flex flex-col gap-2">
            <p className="text-sm text-fg-muted">
              <Link href="/explore" className="hover:text-fg hover:underline">
                Explore
              </Link>{" "}
              / {repo.owner}
            </p>
            <h1 className="font-mono text-2xl font-semibold tracking-tight">{full}</h1>
          </header>

          <section id="overview" aria-labelledby="overview-title" className="flex flex-col gap-3">
            <h2 id="overview-title" className="text-lg font-semibold">
              What it does
            </h2>
            {overview.summary ? (
              <>
                <p className="leading-relaxed">{overview.summary.text}</p>
                {overview.summary.directories.length > 0 && (
                  <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
                    {overview.summary.directories.map((dir) => (
                      <div key={dir.path} className="contents">
                        <dt>
                          <a
                            href={githubTree(full, repo.sha, dir.path)}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="font-mono text-fg-muted hover:text-fg hover:underline"
                          >
                            {dir.path}/
                          </a>
                        </dt>
                        <dd className="text-fg-muted">{dir.summary}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </>
            ) : (
              <p className="text-fg-muted">
                This repository wasn&apos;t summarized when it was indexed.
              </p>
            )}
          </section>

          <section id="layout" aria-labelledby="layout-title" className="flex flex-col gap-3">
            <h2 id="layout-title" className="text-lg font-semibold">
              How it&apos;s laid out
            </h2>
            <ul className="flex flex-col divide-y divide-border rounded-lg border border-border text-sm">
              {overview.architecture.modules.map((module) => (
                <li key={module.path} className="flex flex-wrap items-center gap-2 px-3 py-2">
                  <a
                    href={githubTree(full, repo.sha, module.path)}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="font-mono hover:underline"
                    title={module.path}
                  >
                    {moduleLabel(module.path)}
                  </a>
                  <span className="text-xs text-fg-muted">{COUNT.format(module.files)} files</span>
                  <span className="ml-auto flex gap-1">
                    {LAYERS.filter((layer) => (module.layers[layer] ?? 0) > 0).map((layer) => (
                      <LayerBadge key={layer} layer={layer} />
                    ))}
                  </span>
                </li>
              ))}
            </ul>
            {overview.architecture.rest > 0 && (
              <p className="text-xs text-fg-muted">
                And {COUNT.format(overview.architecture.rest)} files in smaller modules.
              </p>
            )}
          </section>

          {maps.length > 0 && (
            <section id="maps" aria-labelledby="maps-title" className="flex flex-col gap-6">
              <h2 id="maps-title" className="text-lg font-semibold">
                Feature maps
              </h2>
              {maps.map((map, i) => (
                <MapSection
                  key={map.key}
                  map={map}
                  id={`map-${i + 1}`}
                  full={full}
                  sha={repo.sha}
                />
              ))}
            </section>
          )}
        </article>

        <aside
          className="flex flex-col gap-6 self-start text-sm lg:sticky lg:top-6"
          aria-label="About this page"
        >
          <div className="flex flex-col gap-1">
            <p className="text-xs font-medium tracking-wide text-fg-faint uppercase">Freshness</p>
            <p>
              Verified at{" "}
              <a
                href={githubCommit(full, repo.sha)}
                target="_blank"
                rel="noreferrer noopener"
                className="font-mono underline hover:text-fg"
                data-testid="verified-sha"
              >
                {shortSha(repo.sha)}
              </a>
              {repo.indexed_at && <> on {DAY.format(new Date(repo.indexed_at))}</>}.
            </p>
            <p className="text-xs text-fg-muted">
              Every line on this page, and every answer, is read at this commit.
            </p>
          </div>
          {languageShares(repo.languages).length > 0 && (
            <div className="flex flex-col gap-1.5">
              <p className="text-xs font-medium tracking-wide text-fg-faint uppercase">Languages</p>
              <LanguageBar shares={languageShares(repo.languages)} />
            </div>
          )}
          <div className="flex flex-col gap-1">
            <p className="text-xs font-medium tracking-wide text-fg-faint uppercase">Indexed</p>
            <p className="text-fg-muted">
              {repo.files !== null && `${COUNT.format(repo.files)} files`}
              {repo.loc !== null && `, ${COUNT.format(repo.loc)} lines`}
              {overview.health.edges.total > 0 &&
                `; ${COUNT.format(overview.health.edges.total)} links in its graph, ${Math.round(
                  (overview.health.edges.precise / overview.health.edges.total) * 100,
                )}% of them exact`}
              .
            </p>
          </div>
          <a
            href={`https://github.com/${full}`}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex items-center gap-1 text-fg-muted hover:text-fg"
          >
            On GitHub <ExternalLink aria-hidden className="size-3.5" />
          </a>
          <Link href="/login" className={buttonVariants({ variant: "outline", size: "sm" })}>
            Index your own repository
          </Link>
        </aside>
      </div>
      <ExploreAsk
        owner={repo.owner}
        name={repo.name}
        questions={overview.questions}
        human={human}
      />
    </>
  );
}

function MapSection({
  map,
  id,
  full,
  sha,
}: {
  map: FeatureMap;
  id: string;
  full: string;
  sha: string;
}) {
  const entries = mapSymbols(map.entry_points);
  const symbols = mapSymbols(map.key_symbols);
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="flex scroll-mt-6 flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 id={`${id}-title`} className="font-semibold">
          {map.title}
        </h3>
        <a
          href={githubTree(full, sha, map.path)}
          target="_blank"
          rel="noreferrer noopener"
          className="font-mono text-xs text-fg-muted hover:text-fg hover:underline"
        >
          {map.path}/
        </a>
        {map.stale && (
          <span className="rounded-sm border border-border px-1.5 text-xs text-fg-muted">
            may be out of date
          </span>
        )}
      </div>
      <p className="text-fg-muted">{map.summary}</p>
      {entries.length > 0 && (
        <Symbols title="Where it starts" items={entries} full={full} sha={sha} />
      )}
      {symbols.length > 0 && (
        <Symbols title="What it rests on" items={symbols} full={full} sha={sha} />
      )}
      {map.data_flow.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium text-fg-faint">How data moves through it</p>
          <ol className="list-decimal pl-5 text-sm text-fg-muted">
            {map.data_flow.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}

function Symbols({
  title,
  items,
  full,
  sha,
}: {
  title: string;
  items: readonly MapSymbol[];
  full: string;
  sha: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-medium text-fg-faint">{title}</p>
      <ul className="flex flex-col gap-1.5">
        {items.map((item) => (
          <li key={item.symbolKey} className="flex flex-wrap items-baseline gap-x-2 text-sm">
            <a
              href={githubBlob(full, sha, item.path, item.lines)}
              target="_blank"
              rel="noreferrer noopener"
              data-testid="source-chip"
              className="inline-flex items-center gap-1 rounded-sm border border-border bg-muted px-1.5 py-0.5 font-mono text-xs hover:text-fg"
              title={`${item.path}, lines ${item.lines[0]}–${item.lines[1]}, on GitHub`}
            >
              {item.name}
              <span className="text-fg-faint">
                {item.path}:{item.lines[0]}–{item.lines[1]}
              </span>
            </a>
            {item.why && <span className="text-fg-muted">{item.why}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}
