import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { http, HttpResponse } from "msw";
import { expect, within } from "storybook/test";

import { OrgHome } from "@/components/orgs/org-home";
import { RepoReady } from "@/components/overview/repo-ready";
import type { Home } from "@/lib/api/overview";
import type { IndexJob, Repo } from "@/lib/api/types";
import { API_REPO_ID, db } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";
import { jobsOf, jobView } from "@/mocks/jobs";
import { mockOverview } from "@/mocks/overview";
import { mockRun, mockRunSteps } from "@/mocks/runs";

const repo = (): Repo => {
  const first = db.repos.acme?.[0];
  if (!first) throw new Error("the mock seed has no repository");
  return first;
};

const latest = (): IndexJob => {
  const first = jobsOf(API_REPO_ID)[0];
  if (!first) throw new Error("the mock seed has no index run");
  return jobView(first);
};

const overview = (without: boolean) =>
  http.get("*/api/v1/orgs/:org/repos/:repo/overview", () =>
    HttpResponse.json(mockOverview(without)),
  );

function Ready() {
  return <RepoReady org="acme" repo="api" data={repo()} latest={latest()} />;
}

const meta = {
  title: "Overview/Repository overview",
  component: Ready,
  parameters: {
    nextjs: {
      appDirectory: true,
      navigation: {
        pathname: "/acme/api",
        segments: [
          ["org", "acme"],
          ["repo", "api"],
        ],
      },
    },
    msw: [overview(false), ...handlers],
  },
} satisfies Meta<typeof Ready>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Indexed with summaries: what it does, its modules on a map and in a table, questions, its
 * feature maps and how well the index understood it. */
export const Indexed: Story = {
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("heading", { name: "What this repo does" })).toBeVisible();
    const modules = canvas.getByRole("table", { name: "Modules" });
    await expect(within(modules).getByRole("link", { name: "frontend/" })).toHaveAttribute(
      "href",
      "/acme/api/code/frontend",
    );
    await expect(
      canvas.getByRole("link", { name: "What happens when POST /api/v1/items/ is called?" }),
    ).toHaveAttribute("href", expect.stringContaining("/acme/api/ask?q=") as string);
    await expect(canvas.getByText("Out of date")).toBeVisible();
    await expect(canvas.getByText("Not tracked yet")).toBeVisible();
  },
};

/** Indexed without the summaries stage: no summary or feature maps, and links unmeasured, each
 * said rather than made up. */
export const WithoutSummaries: Story = {
  parameters: { msw: [overview(true), ...handlers] },
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByText(/No summary: the summaries stage didn.t run/),
    ).toBeVisible();
    await expect(canvas.queryByRole("heading", { name: "Feature maps" })).toBeNull();
    await expect(canvas.getByText("Not measured at this index")).toBeVisible();
  },
};

const homeOf = (body: Partial<Home>) =>
  http.get("*/api/v1/orgs/:org/home", () =>
    HttpResponse.json<Home>({ repos: [repo()], live: [], recent: [], ...body }),
  );

const { steps: _, ...running } = mockRun(mockRunSteps().slice(0, 6), "running");

export const OrgHomePage: StoryObj<typeof OrgHome> = {
  render: () => <OrgHome />,
  parameters: {
    nextjs: {
      appDirectory: true,
      navigation: { pathname: "/acme", segments: [["org", "acme"]] },
    },
    msw: [
      homeOf({
        live: [{ ...running, repo_id: repo().id, user: "hubot" }],
        recent: [
          {
            id: "0199f000-0000-7000-8000-0000000000c1",
            repo_id: repo().id,
            title: "How are items saved?",
            shared: false,
            created_at: "2026-10-18T11:40:00Z",
            updated_at: "2026-10-18T11:40:00Z",
          },
        ],
      }),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    const ask = await canvas.findByRole("form", { name: "Ask anything" });
    await expect(within(ask).getByRole("button", { name: "Ask" })).toBeDisabled();
    await expect(canvas.getByRole("link", { name: /How is an item created\?/ })).toHaveAttribute(
      "href",
      expect.stringContaining("/acme/api/runs/") as string,
    );
    await expect(canvas.getByRole("link", { name: /How are items saved\?/ })).toHaveAttribute(
      "href",
      "/acme/api/ask/0199f000-0000-7000-8000-0000000000c1",
    );
  },
};
