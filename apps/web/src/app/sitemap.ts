import type { MetadataRoute } from "next";
import { connection } from "next/server";

import { publicRepos, siteOrigin } from "@/lib/api/explore-server";
import { explorePath } from "@/lib/routes";

/** The public pages crawlers may list: the landing page, the gallery and each published
 * repository's page (at its last index). Read when asked, not at build: it is the API's. */
export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  await connection();
  const origin = siteOrigin();
  const repos = await publicRepos();
  return [
    { url: `${origin}/`, changeFrequency: "weekly" },
    { url: `${origin}/explore`, changeFrequency: "daily" },
    ...repos.map((repo) => ({
      url: `${origin}${explorePath(repo.owner, repo.name)}`,
      ...(repo.indexed_at ? { lastModified: repo.indexed_at } : {}),
      changeFrequency: "weekly" as const,
    })),
  ];
}
