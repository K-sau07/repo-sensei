/**
 * The public gallery (10.2): each published repository as a card (what it does, its languages,
 * its size, the commit it was verified at), and a way to index one's own.
 */
import { cn } from "cn";
import Link from "next/link";

import { buttonVariants } from "@/components/ui/button";
import { languageShares, shortSha, type PublicRepo } from "@/lib/api/explore";
import { explorePath } from "@/lib/routes";

const DAY = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" });
const COUNT = new Intl.NumberFormat("en");

export function Gallery({ repos }: { repos: readonly PublicRepo[] }) {
  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 px-6 py-10">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex max-w-2xl flex-col gap-2">
          <h1 className="text-3xl font-semibold tracking-tight">Explore</h1>
          <p className="text-fg-muted">
            Open-source repositories, indexed: what each one does, how it&apos;s laid out, and
            answers that cite the lines they stand on.
          </p>
        </div>
        <Link href="/login" className={buttonVariants({ variant: "default" })}>
          Index your own repository
        </Link>
      </header>
      {repos.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border p-10 text-center text-fg-muted">
          Nothing is published here yet.
        </p>
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" aria-label="Repositories">
          {repos.map((repo) => (
            <li key={repo.full_name}>
              <RepoCard repo={repo} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RepoCard({ repo }: { repo: PublicRepo }) {
  const shares = languageShares(repo.languages);
  return (
    <Link
      href={explorePath(repo.owner, repo.name)}
      className="flex h-full flex-col gap-3 rounded-lg border border-border p-4 transition-colors hover:border-line-strong hover:bg-muted/40"
    >
      <h2 className="font-mono text-sm font-medium">
        <span className="text-fg-muted">{repo.owner}/</span>
        {repo.name}
      </h2>
      <p className={cn("line-clamp-3 text-sm", repo.summary ? "text-fg" : "text-fg-faint")}>
        {repo.summary ?? "Not summarized yet."}
      </p>
      {shares.length > 0 && <LanguageBar shares={shares} />}
      <p className="mt-auto text-xs text-fg-muted">
        {repo.files !== null && `${COUNT.format(repo.files)} files`}
        {repo.loc !== null && ` · ${COUNT.format(repo.loc)} lines`}
        <br />
        Verified at <span className="font-mono">{shortSha(repo.sha)}</span>
        {repo.indexed_at && ` on ${DAY.format(new Date(repo.indexed_at))}`}
      </p>
    </Link>
  );
}

export function LanguageBar({
  shares,
}: {
  shares: readonly { language: string; share: number }[];
}) {
  const said = shares.map((s) => `${s.language} ${Math.round(s.share * 100)}%`).join(", ");
  return (
    <div className="flex flex-col gap-1.5">
      <div
        className="flex h-1.5 overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label={said}
      >
        {shares.map((s, i) => (
          <span
            key={s.language}
            className="h-full"
            style={{
              width: `${s.share * 100}%`,
              backgroundColor: `var(--color-chart-${(i % 5) + 1})`,
            }}
          />
        ))}
      </div>
      <p className="text-xs text-fg-muted" aria-hidden>
        {said}
      </p>
    </div>
  );
}
