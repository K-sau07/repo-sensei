import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { useState } from "react";
import { expect, screen, userEvent, waitFor, within } from "storybook/test";

import { IndexView } from "@/components/indexing/index-view";
import { LogViewer } from "@/components/indexing/log-viewer";
import type { LogLine } from "@/components/indexing/use-job-events";
import type { Stage } from "@/lib/api/types";
import { API_REPO_ID, db, resetMockDb, saveDb } from "@/mocks/db";
import { RUN_LENGTH_MS, type MockJob } from "@/mocks/jobs";

const nextjs = {
  appDirectory: true,
  navigation: {
    pathname: "/acme/api",
    segments: [
      ["org", "acme"],
      ["repo", "api"],
    ],
  },
};

/** Put a job on the seeded repository, queued `ago` ms before the story starts. */
function seedJob(id: string, ago: number, extra: Partial<MockJob> = {}) {
  resetMockDb();
  const now = Date.now() - ago;
  db.jobs[API_REPO_ID] = [
    {
      id,
      repo_id: API_REPO_ID,
      created_at: new Date(now).toISOString(),
      queued_ms: now,
      ...extra,
    },
  ];
  saveDb();
}

const meta = {
  title: "Indexing/Index run",
  component: IndexView,
  args: { org: "acme", repo: "api", jobId: "job-story" },
  parameters: { nextjs },
} satisfies Meta<typeof IndexView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The recorded run of the fastapi template, replayed live from the start. */
export const Running: Story = {
  beforeEach: () => {
    seedJob("job-story", 0);
  },
  play: async ({ canvas }) => {
    const stages = await canvas.findByRole("list", { name: "Indexing stages" });
    await expect(stages).toBeVisible();
    // The stream carries it to the end: discovery's counts, the unlocks, then "Indexed".
    await waitFor(
      async () => {
        await expect(canvas.getByRole("heading", { name: /^Indexed/ })).toBeVisible();
      },
      { timeout: RUN_LENGTH_MS + 5000 },
    );
    await waitFor(async () => {
      await expect(canvas.getByText("245")).toBeVisible();
    });
    await expect(canvas.getByRole("list", { name: "Now available" })).toHaveTextContent(
      "Code browser available now",
    );
    await expect(canvas.getByRole("button", { name: "Index again" })).toBeVisible();
  },
};

export const Succeeded: Story = {
  beforeEach: () => {
    seedJob("job-story", RUN_LENGTH_MS + 60_000);
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("heading", { name: /^Indexed/ })).toHaveTextContent(
      "100%",
    );
    const stages = canvas.getByRole("list", { name: "Indexing stages" });
    await expect(within(stages).getAllByText("done").length).toBeGreaterThan(0);
    await expect(canvas.getByRole("heading", { name: "What we found" })).toBeVisible();
    // Opening a finished run replays its history for the log, and nothing else changes.
    await expect(await canvas.findByRole("button", { name: /View logs \([1-9]/ })).toBeVisible();
    await expect(canvas.queryByRole("button", { name: "Cancel indexing" })).toBeNull();
  },
};

export const Failed: Story = {
  beforeEach: () => {
    seedJob("job-story", RUN_LENGTH_MS + 60_000, {
      failure: {
        stage: "clone",
        code: "repo_too_large",
        message: "The repository is over 2.0 GB checked out, the limit.",
      },
    });
  },
  play: async ({ canvas }) => {
    const alert = await canvas.findByRole("alert");
    await expect(alert).toHaveTextContent("The repository is over 2.0 GB checked out, the limit.");
    await expect(alert).toHaveTextContent("repo_too_large");
    await expect(await within(alert).findByRole("button", { name: "Retry" })).toBeVisible();
  },
};

export const Cancelled: Story = {
  beforeEach: () => {
    seedJob("job-story", RUN_LENGTH_MS + 60_000, { cancelled_after_ms: 1500 });
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("heading", { name: /^Index run/ })).toBeVisible();
    await expect(
      within(canvas.getByRole("list", { name: "Indexing stages" })).getByText("cancelled"),
    ).toBeInTheDocument();
    await expect(await canvas.findByRole("button", { name: "Index again" })).toBeVisible();
  },
};

const STAGES: Stage[] = ["clone", "discover", "merkle"];
const LEVELS = ["info", "info", "info", "warn", "error"] as const;

function longLog(count: number): LogLine[] {
  const start = Date.parse("2026-10-20T12:00:00Z");
  return Array.from({ length: count }, (_, index) => ({
    id: `line-${String(index)}`,
    ts: new Date(start + index * 40).toISOString(),
    level: LEVELS[index % LEVELS.length] ?? "info",
    stage: STAGES[index % STAGES.length] ?? null,
    message: `Line ${String(index + 1)}: read src/module_${String(index % 97)}.py`,
  }));
}

function LongLog() {
  const [open, setOpen] = useState(true);
  return <LogViewer open={open} onOpenChange={setOpen} lines={longLog(5000)} />;
}

/** 5,000 lines stay smooth: only what's in view is in the DOM. */
export const FiveThousandLineLog: StoryObj = {
  render: () => <LongLog />,
  play: async () => {
    const log = await screen.findByRole("region", { name: "Log lines" });
    await expect(screen.getByText("5000 lines")).toBeVisible();
    await expect(within(log).getAllByRole("listitem").length).toBeLessThan(120);
    await userEvent.click(screen.getByRole("button", { name: "info" }));
    await userEvent.click(screen.getByRole("button", { name: "warn" }));
    await expect(await screen.findByText("1000 lines")).toBeVisible();
    await userEvent.type(screen.getByLabelText("Search the log"), "line 4995:");
    await expect(await screen.findByText("1 line")).toBeVisible();
    await expect(within(log).getByText(/Line 4995:/)).toBeVisible();
  },
};
