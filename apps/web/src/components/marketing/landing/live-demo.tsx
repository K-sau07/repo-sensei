/**
 * The landing page's live demo (10.3): a published open-source repository, asked about for real
 * (the public Ask: its suggested questions, answers streamed with citations to the lines on
 * GitHub), over a constellation backdrop. Without a published repository it points to Explore.
 */
import Link from "next/link";

import { ConstellationBackdrop } from "@/components/auth/constellation-backdrop";
import { ExploreAsk, type HumanCheck } from "@/components/explore/explore-ask";
import { buttonVariants } from "@/components/ui/button";
import { shortSha, type PublicRepoPage } from "@/lib/api/explore";
import { explorePath } from "@/lib/routes";

export function LiveDemo({ page, human }: { page: PublicRepoPage | null; human: HumanCheck }) {
  return (
    <section
      aria-labelledby="demo-title"
      className="relative isolate overflow-hidden rounded-2xl border border-line bg-surface-1"
    >
      <ConstellationBackdrop />
      <div className="relative flex flex-col gap-4 p-5 sm:p-8">
        <h2 id="demo-title" className="text-sm font-medium text-fg-muted">
          Try it on a real repository
        </h2>
        {page ? <Demo page={page} human={human} /> : <NoDemo />}
      </div>
    </section>
  );
}

function Demo({ page, human }: { page: PublicRepoPage; human: HumanCheck }) {
  const { repo, overview } = page;
  return (
    <>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-mono text-lg font-medium">
          <span className="text-fg-muted">{repo.owner}/</span>
          {repo.name}
        </p>
        <p className="text-xs text-fg-muted">
          Indexed at <span className="font-mono">{shortSha(repo.sha)}</span> ·{" "}
          <Link href={explorePath(repo.owner, repo.name)} className="underline hover:text-fg">
            its page
          </Link>
        </p>
      </div>
      {repo.summary && <p className="max-w-3xl text-sm text-fg-muted">{repo.summary}</p>}
      <ExploreAsk
        inline
        owner={repo.owner}
        name={repo.name}
        questions={overview.questions}
        human={human}
      />
    </>
  );
}

function NoDemo() {
  return (
    <div className="flex flex-col items-start gap-3">
      <p className="max-w-xl text-sm text-fg-muted">
        Open-source repositories indexed here are in Explore: what each does, how it&apos;s laid
        out, and answers that cite their lines.
      </p>
      <Link href="/explore" className={buttonVariants({ variant: "outline" })}>
        Explore repositories
      </Link>
    </div>
  );
}
