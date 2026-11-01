"use client";

/**
 * The Packages tab (6.7): the third-party packages the manifests declare, by ecosystem, with
 * their locked versions, where each is declared (a development dependency marked), and the
 * files importing it most; then what is imported but declared nowhere.
 */
import { AlertTriangle, Package as PackageIcon } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import type { Package } from "@/lib/api/deps";
import { codePath } from "@/lib/routes";

import { usePackages } from "./use-deps";

const ECOSYSTEMS: Record<string, string> = { pypi: "Python (PyPI)", npm: "JavaScript (npm)" };

/** The packages by ecosystem, the most imported first, those matching `query`. */
export function clustered(packages: readonly Package[], query: string): [string, Package[]][] {
  const wanted = query.trim().toLowerCase();
  const groups = new Map<string, Package[]>();
  for (const p of packages) {
    if (wanted && !p.name.toLowerCase().includes(wanted)) continue;
    const listed = groups.get(p.ecosystem);
    if (listed) listed.push(p);
    else groups.set(p.ecosystem, [p]);
  }
  for (const listed of groups.values()) {
    listed.sort((a, b) => b.files - a.files || a.name.localeCompare(b.name));
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

export function PackagesView() {
  const packages = usePackages();
  const [query, setQuery] = useState("");
  const groups = useMemo(
    () => clustered(packages.data?.packages ?? [], query),
    [packages.data, query],
  );
  if (packages.isPending) return <Skeleton className="m-4 h-60" />;
  if (packages.isError) {
    return (
      <div className="p-4">
        <ErrorState title="Couldn't load the packages" onRetry={() => void packages.refetch()} />
      </div>
    );
  }
  const { inventoried, undeclared } = packages.data;
  return (
    <div className="flex max-w-4xl flex-col gap-6 p-4">
      {!inventoried ? (
        <p className="text-sm text-fg-muted">
          This commit was indexed before packages were read from its manifests: re-index it to see
          their versions.
        </p>
      ) : null}
      <Input
        aria-label="Find a package"
        placeholder="Find a package"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
        }}
        className="h-8 max-w-xs"
      />
      {groups.map(([ecosystem, listed]) => (
        <section
          key={ecosystem}
          aria-labelledby={`packages-${ecosystem}`}
          className="flex flex-col gap-2"
        >
          <h2 id={`packages-${ecosystem}`} className="text-sm font-medium">
            {ECOSYSTEMS[ecosystem] ?? ecosystem}{" "}
            <span className="text-fg-muted tabular-nums">({listed.length})</span>
          </h2>
          <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
            {listed.map((p) => (
              <PackageRow key={`${p.ecosystem}:${p.name}`} found={p} />
            ))}
          </ul>
        </section>
      ))}
      {undeclared.length > 0 ? (
        <section aria-labelledby="packages-undeclared" className="flex flex-col gap-2">
          <h2 id="packages-undeclared" className="flex items-center gap-1.5 text-sm font-medium">
            <AlertTriangle aria-hidden className="size-4 text-warn" />
            Imported, declared nowhere
          </h2>
          <ul className="flex flex-col divide-y divide-line rounded-md border border-line">
            {undeclared.map((p) => (
              <PackageRow key={`${p.ecosystem}:${p.name}`} found={p} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function PackageRow({ found }: { found: Package }) {
  const { org, repo } = useRepoSlug();
  const declared = found.declared[0];
  return (
    <li className="px-3 py-2" data-testid="package">
      <details>
        <summary className="flex cursor-pointer items-center gap-3 text-sm">
          <PackageIcon aria-hidden className="size-4 text-fg-muted" />
          <span className="min-w-0 flex-1 truncate font-mono">{found.name}</span>
          {found.dev ? <Badge variant="outline">dev</Badge> : null}
          <span className="font-mono text-xs text-fg-muted">
            {found.versions.length > 0 ? found.versions.join(", ") : (declared?.spec ?? "unpinned")}
          </span>
          <span className="w-24 text-right text-xs text-fg-muted tabular-nums">
            {found.files} {found.files === 1 ? "file" : "files"}
          </span>
        </summary>
        <div className="mt-2 flex flex-col gap-2 pl-7 text-xs">
          {found.declared.length > 0 ? (
            <p className="text-fg-muted">
              Declared in{" "}
              {found.declared.map((d, i) => (
                <span key={d.manifest}>
                  {i > 0 ? ", " : ""}
                  <Link
                    href={codePath(org, repo, d.manifest)}
                    className="underline underline-offset-4"
                  >
                    {d.manifest}
                  </Link>
                  {d.spec ? ` (${d.spec})` : ""}
                  {d.dev ? " for development" : ""}
                </span>
              ))}
            </p>
          ) : null}
          {found.used_in.length > 0 ? (
            <ul className="flex flex-col gap-0.5" aria-label={`Files importing ${found.name}`}>
              {found.used_in.map((u) => (
                <li key={u.path} className="flex gap-2">
                  <Link
                    href={codePath(org, repo, u.path)}
                    className="min-w-0 flex-1 truncate font-mono hover:underline"
                  >
                    {u.path}
                  </Link>
                  <span className="text-fg-muted tabular-nums">{u.imports}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-fg-faint">Declared, but nothing in the code imports it.</p>
          )}
        </div>
      </details>
    </li>
  );
}
