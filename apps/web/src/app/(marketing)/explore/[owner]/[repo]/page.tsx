import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { RepoPage } from "@/components/explore/repo-page";
import type { PublicRepoPage } from "@/lib/api/explore";
import { humanCheck, publicMaps, publicRepo, siteOrigin } from "@/lib/api/explore-server";
import { explorePath } from "@/lib/routes";

type Props = PageProps<"/explore/[owner]/[repo]">;

const DESCRIPTION_CHARS = 160;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { owner, repo } = await params;
  const page = await publicRepo(owner, repo);
  if (page === null) return { title: "Not found", robots: { index: false } };
  const full = page.repo.full_name;
  const description = clip(
    page.repo.summary ?? `${full}, indexed by repo-sensei: what it does and how it's laid out.`,
  );
  const url = explorePath(page.repo.owner, page.repo.name);
  return {
    title: full,
    description,
    alternates: { canonical: url },
    openGraph: { title: `${full} · repo-sensei`, description, url, type: "website" },
    twitter: { card: "summary_large_image", title: full, description },
  };
}

// Read before anything is sent (not streamed behind a boundary): a repository that isn't
// published answers 404, and a crawler gets the whole page at once.
export const instant = false;

export default async function ExploreRepoPage({ params }: Props) {
  const { owner, repo } = await params;
  // The maps are a part of the page: without them (the API failing them), it shows the rest.
  const [page, maps] = await Promise.all([
    publicRepo(owner, repo),
    publicMaps(owner, repo).catch(() => null),
  ]);
  if (page === null) notFound();
  return (
    <>
      <script
        type="application/ld+json"
        // A data block, never run; "<" escaped so no text in it can close the element.
        dangerouslySetInnerHTML={{
          __html: JSON.stringify(structured(page)).replaceAll("<", "\\u003c"),
        }}
      />
      <RepoPage page={page} maps={maps?.maps ?? []} human={humanCheck()} />
    </>
  );
}

/** What search engines read of the page (schema.org's SoftwareSourceCode). */
function structured(page: PublicRepoPage): Record<string, unknown> {
  const { repo } = page;
  return {
    "@context": "https://schema.org",
    "@type": "SoftwareSourceCode",
    name: repo.full_name,
    url: `${siteOrigin()}${explorePath(repo.owner, repo.name)}`,
    codeRepository: `https://github.com/${repo.full_name}`,
    programmingLanguage: Object.keys(repo.languages),
    ...(repo.summary ? { description: repo.summary } : {}),
    ...(repo.indexed_at ? { dateModified: repo.indexed_at } : {}),
    version: repo.sha,
  };
}

function clip(text: string): string {
  const words = text.replace(/\s+/g, " ").trim();
  return words.length <= DESCRIPTION_CHARS ? words : `${words.slice(0, DESCRIPTION_CHARS - 1)}…`;
}
