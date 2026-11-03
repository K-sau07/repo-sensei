/**
 * The mock public gallery (10.2): the mock snapshot (the fastapi template at b1125d8) published
 * by the explore org, with its overview and feature maps, and a second repository that is only
 * a card. Read by the server's fetchers in mock mode (lib/api/explore-server.ts), as there is no
 * API behind them then. The maps' lines are the mock snapshot's.
 */
import type { FeatureMapList, PublicRepo, PublicRepoPage } from "@/lib/api/explore";

import { BROWSE_COMMIT } from "./browse";
import { mockOverview } from "./overview";

const TEMPLATE: PublicRepo = {
  owner: "fastapi",
  name: "full-stack-fastapi-template",
  full_name: "fastapi/full-stack-fastapi-template",
  default_branch: "master",
  languages: { TypeScript: 412_000, Python: 238_000, HTML: 31_000, Dockerfile: 2_400 },
  sha: BROWSE_COMMIT,
  indexed_at: "2026-10-18T11:30:00Z",
  files: 187,
  loc: 24_310,
  summary: mockOverview(false).summary?.text ?? null,
  published_at: "2026-10-20T09:00:00Z",
};

const CLI: PublicRepo = {
  owner: "tiangolo",
  name: "typer",
  full_name: "tiangolo/typer",
  default_branch: "master",
  languages: { Python: 610_000, Shell: 3_100 },
  sha: "4f1e8a2c9b7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f",
  indexed_at: "2026-10-19T08:00:00Z",
  files: 512,
  loc: 41_870,
  summary: null,
  published_at: "2026-10-19T09:00:00Z",
};

export const MOCK_PUBLIC_REPOS: PublicRepo[] = [TEMPLATE, CLI];

export function mockPublicPage(owner: string, name: string): PublicRepoPage | null {
  const repo = MOCK_PUBLIC_REPOS.find(
    (r) => r.full_name.toLowerCase() === `${owner}/${name}`.toLowerCase(),
  );
  if (repo === undefined) return null;
  const overview = { ...mockOverview(false), sha: repo.sha, index: null };
  if (repo !== TEMPLATE) {
    return { repo, overview: { ...overview, summary: null, feature_maps: [], questions: [] } };
  }
  return { repo, overview };
}

const ROUTES = "backend/app/api/routes";

export function mockPublicMaps(owner: string, name: string): FeatureMapList | null {
  const page = mockPublicPage(owner, name);
  if (page === null) return null;
  if (page.repo !== TEMPLATE) return { sha: page.repo.sha, maps: [] };
  return {
    sha: page.repo.sha,
    maps: page.overview.feature_maps.map((card, i) => ({
      id: `0199f000-0000-7000-8000-00000000f0${i}0`,
      key: card.key,
      path: card.path,
      title: card.title,
      summary: card.summary,
      stale: card.stale,
      entry_points:
        card.path === "backend/app/api"
          ? [
              {
                symbol_key: `py ${ROUTES}/items.py/create_item().`,
                why: "Creates an item for the signed-in user.",
                path: `${ROUTES}/items.py`,
                start_line: 61,
                end_line: 72,
              },
              {
                symbol_key: `py ${ROUTES}/login.py/login_access_token().`,
                why: "Trades a username and password for an access token.",
                path: `${ROUTES}/login.py`,
                start_line: 23,
                end_line: 42,
              },
            ]
          : [],
      key_symbols:
        card.path === "backend/app/api"
          ? [
              {
                symbol_key: "py backend/app/api/deps.py/get_current_user().",
                why: "Every route that needs a user reads it here.",
                path: "backend/app/api/deps.py",
                start_line: 30,
                end_line: 49,
              },
            ]
          : [],
      data_flow:
        card.path === "backend/app/api"
          ? ["A request reaches its router", "deps read the user", "crud writes the row"]
          : [],
      citations: [],
    })),
  };
}
