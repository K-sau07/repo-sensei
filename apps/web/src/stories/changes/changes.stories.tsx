import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { http, HttpResponse } from "msw";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { ChangePage } from "@/components/changes/change-page";
import { ChangesPage } from "@/components/changes/changes-page";
import { NewChangePage } from "@/components/changes/new-change-page";
import type { IndexJob } from "@/lib/api/types";
import { db, OCTO } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";
import { meOf } from "@/mocks/handlers/me";

const WAITING = "0199f000-0000-7000-8000-00000000c001";
const LOAD_MS = 10_000;

const navigation = (section: string, changeId?: string) => ({
  appDirectory: true,
  navigation: {
    pathname: `/acme/api/${section}`,
    segments: [["org", "acme"], ["repo", "api"], ...(changeId ? [["changeId", changeId]] : [])],
  },
});

/** The repository indexed already, changes unlocked (the mock otherwise replays a run in time). */
const indexed = http.get("*/api/v1/orgs/:org/repos/:repo/index-jobs", () =>
  HttpResponse.json<IndexJob[]>([
    {
      id: "0199f000-0000-7000-8000-0000000000ad",
      repo_id: "0199f000-0000-7000-8000-0000000000ae",
      kind: "full",
      status: "succeeded",
      stage: null,
      progress: 1,
      eta_s: null,
      features: ["keyword_search", "ask", "graph_view", "trace", "diagrams", "changes"],
      partial: {},
      stages: {},
      error: null,
      snapshot_id: "0199f000-0000-7000-8000-0000000000af",
      created_at: "2026-10-18T11:00:00Z",
      started_at: "2026-10-18T11:00:00Z",
      finished_at: "2026-10-18T11:05:00Z",
    },
  ]),
);

/** The signed-in user (Octo) seen as a viewer of Acme. */
const asViewer = http.get("*/api/v1/me", () => {
  const octo = db.users.find((u) => u.id === OCTO);
  if (!octo) throw new Error("mock seed is missing Octo");
  const me = meOf(octo);
  return HttpResponse.json({
    ...me,
    orgs: me.orgs.map((o) => (o.slug === "acme" ? { ...o, role: "viewer" as const } : o)),
  });
});

const meta = {
  title: "Changes/Changes",
  component: ChangesPage,
  parameters: { nextjs: navigation("changes"), msw: [indexed, ...handlers] },
} satisfies Meta<typeof ChangesPage>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The repository's changes: waiting for approval, a draft PR, rejected. */
export const List: Story = {
  play: async ({ canvas }) => {
    const rows = await canvas.findAllByTestId("change-row", {}, { timeout: LOAD_MS });
    await expect(rows).toHaveLength(3);
    await expect(within(rows[0] ?? document.body).getByTestId("fix-status")).toHaveTextContent(
      "Awaiting approval",
    );
    await expect(canvas.getByRole("link", { name: /Draft PR/ })).toHaveAttribute(
      "href",
      "https://github.com/acme/api/pull/57",
    );
    await expect(canvas.getByRole("link", { name: "New change" })).toBeVisible();
  },
};

/** The plan canvas: files by layer (a new one marked), its test, the pattern it follows, its
 * blast radius; a member edits it in place, and an edited plan is planned again, not approved. */
export const PlanCanvas: StoryObj<typeof ChangePage> = {
  render: () => <ChangePage />,
  parameters: { nextjs: navigation(`changes/${WAITING}`, WAITING) },
  play: async ({ canvas }) => {
    const plan = await canvas.findByTestId("plan-canvas", {}, { timeout: LOAD_MS });
    const layers = within(plan).getAllByTestId("layer");
    await expect(layers.map((l) => l.dataset.layer)).toEqual(["ui", "api", "svc"]);
    await expect(within(layers[1] ?? plan).getAllByTestId("planned-file")).toHaveLength(2);
    await expect(within(layers[1] ?? plan).getByText("New")).toBeVisible();
    await expect(within(plan).getAllByTestId("planned-test")).toHaveLength(1);
    await expect(
      within(plan).getByRole("link", { name: "backend/app/api/deps.py:21–57" }),
    ).toHaveAttribute("href", "/acme/api/code/backend/app/api/deps.py?L=21-57");
    await expect(
      within(plan).getByRole("img", { name: /What depends on the change: 4 routes/ }),
    ).toBeVisible();
    // A member's controls, once who they are has loaded.
    const approve = await canvas.findByRole("button", { name: "Approve plan" });
    await expect(approve).toBeEnabled();
    await userEvent.click(
      within(plan).getByRole("button", {
        name: "Remove frontend/src/client/core/request.ts from the plan",
      }),
    );
    await waitFor(async () => {
      await expect(approve).toBeDisabled();
    });
    await expect(canvas.getByTestId("edits-pending")).toBeVisible();
    await expect(canvas.getByRole("button", { name: "Plan again with my edits" })).toBeEnabled();
  },
};

/** A viewer reads the plan; it isn't theirs to edit or decide on. */
export const PlanAsViewer: StoryObj<typeof ChangePage> = {
  render: () => <ChangePage />,
  parameters: {
    nextjs: navigation(`changes/${WAITING}`, WAITING),
    msw: [asViewer, ...handlers],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByTestId("plan-canvas", {}, { timeout: LOAD_MS })).toBeVisible();
    await expect(canvas.queryByRole("button", { name: "Approve plan" })).toBeNull();
    await expect(canvas.queryByRole("button", { name: /^Remove / })).toBeNull();
  },
};

/** Describe a change: a suggestion fills it in; layer chips scope it. */
export const Describe: StoryObj<typeof NewChangePage> = {
  render: () => <NewChangePage />,
  parameters: { nextjs: navigation("changes/new") },
  play: async ({ canvas }) => {
    const box = await canvas.findByLabelText("What should change?");
    await expect(canvas.getByRole("button", { name: "Plan this change" })).toBeDisabled();
    await userEvent.click(canvas.getByRole("button", { name: /^Add rate limiting/ }));
    await expect(box).toHaveValue(
      "Add rate limiting to the API: 60 requests a minute per client, 429 after that.",
    );
    await userEvent.click(canvas.getByRole("button", { name: "API" }));
    await expect(canvas.getByRole("button", { name: "API" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(canvas.getByRole("button", { name: "Plan this change" })).toBeEnabled();
  },
};
