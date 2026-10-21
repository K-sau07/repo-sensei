import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { delay, http, HttpResponse } from "msw";
import { expect, screen, userEvent, waitFor } from "storybook/test";

import { RepoList } from "@/components/repos/repo-list";
import type { Repo } from "@/lib/api/types";
import { db } from "@/mocks/db";
import { apiError, handlers } from "@/mocks/handlers";

const nextjs = {
  appDirectory: true,
  navigation: { pathname: "/acme/repos", segments: [["org", "acme"]] },
};

const seed = (): Repo => {
  const first = db.repos.acme?.[0];
  if (!first) throw new Error("the mock seed has no repository");
  return first;
};

const sample: Repo[] = [
  seed(),
  {
    ...seed(),
    id: "repo-web",
    slug: "web",
    name: "web",
    full_name: "acme-gh/web",
    languages: { TypeScript: 300_000, CSS: 20_000 },
    status: "indexing",
    loc: null,
    file_count: null,
    last_indexed_at: null,
    active_job: { id: "job-1", status: "running", stage: "discover", progress: 0.42, eta_s: 90 },
  },
  {
    ...seed(),
    id: "repo-infra",
    slug: "infra",
    name: "infra",
    full_name: "acme-gh/infra",
    languages: { HCL: 50_000 },
    status: "access_lost",
  },
];

const repoList = (repos: Repo[] | (() => Response | Promise<Response>)) =>
  http.get("*/api/v1/orgs/:org/repos", async () =>
    typeof repos === "function" ? repos() : HttpResponse.json(repos),
  );

const listMeta = {
  title: "Repos/Repository list",
  component: RepoList,
  args: { org: "acme" },
  parameters: { nextjs },
} satisfies Meta<typeof RepoList>;

export default listMeta;
type ListStory = StoryObj<typeof listMeta>;

export const List: ListStory = {
  parameters: { msw: [repoList(sample), ...handlers] },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("acme-gh/web · private")).toBeVisible();
    await expect(canvas.getByText("Indexing 42%")).toBeVisible();
    await expect(canvas.getByText("Access lost")).toBeVisible();
    await expect(canvas.getByRole("img", { name: /Python/ })).toBeVisible();
  },
};

export const Empty: ListStory = {
  parameters: { msw: [repoList([]), ...handlers] },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("No repositories yet")).toBeVisible();
    // Admins get the way forward (the role arrives with a second request).
    await expect(await canvas.findByRole("link", { name: "Connect repository" })).toBeVisible();
  },
};

export const Loading: ListStory = {
  parameters: {
    msw: [
      repoList(async () => {
        await delay("infinite");
        return HttpResponse.json([]);
      }),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("status", { name: "Loading repositories" })).toBeVisible();
  },
};

export const Failure: ListStory = {
  parameters: {
    msw: [
      repoList(() => apiError(503, "service_unavailable", "The API is restarting.")),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    // After the client's retries of a 503.
    await expect(
      await canvas.findByText("Couldn't load repositories", undefined, { timeout: 15_000 }),
    ).toBeVisible();
    await expect(canvas.getByText("mock-request-id")).toBeVisible();
  },
};

export const FilteredToNothing: ListStory = {
  parameters: { msw: [repoList(sample), ...handlers] },
  play: async ({ canvas }) => {
    await canvas.findByText("acme-gh/web · private");
    await userEvent.click(canvas.getByRole("combobox", { name: "Filter by language" }));
    await userEvent.click(await screen.findByRole("option", { name: "HCL" })); // in a portal
    await expect(await canvas.findByText("1 of 3")).toBeVisible();
    // Let the list close before the accessibility check (its focus guards are mid-exit).
    await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
  },
};
