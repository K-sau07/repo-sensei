import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, fn, userEvent, waitFor, within } from "storybook/test";

import { NewCommitsNotice } from "@/components/repo/new-commits-banner";
import { RefSwitcher } from "@/components/repo/repo-header";
import { RepoStatus } from "@/components/repos/repo-status";
import { PUSHED_FILES, PUSHED_SHA } from "@/mocks/push";

const navigation = {
  appDirectory: true,
  navigation: {
    pathname: "/acme/api",
    segments: [
      ["org", "acme"],
      ["repo", "api"],
    ],
  },
};

/** Freshness (5.6): which branch a repository's pages are read at, a new index landing while
 * they are open, and a push's incremental run in the status dot. */
const meta = {
  title: "Repos/Freshness",
  component: RefSwitcher,
  parameters: { nextjs: navigation },
  args: { defaultBranch: "main" },
} satisfies Meta<typeof RefSwitcher>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The ref switcher: the default branch, a tracked branch on the same index, and a pull request
 * whose overlay is still being built (listed, not pickable). Picking one reads the page at it. */
export const RefSwitcherOpen: Story = {
  play: async ({ canvas, canvasElement }) => {
    const button = canvas.getByTestId("ref-switcher");
    await expect(button).toHaveTextContent("main");
    await userEvent.click(button);
    const body = within(canvasElement.ownerDocument.body);
    const release = await body.findByRole("option", { name: /release/ });
    await expect(release).toHaveTextContent("Indexed at b1125d8");
    await expect(body.getByRole("option", { name: /pull\/7/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await userEvent.click(release);
    await expect(button).toHaveTextContent("release");
    await waitFor(() => expect(body.queryByRole("option", { name: /release/ })).toBeNull());
  },
};

/** Searching the refs by name. */
export const RefSwitcherSearch: Story = {
  play: async ({ canvas, canvasElement }) => {
    await userEvent.click(canvas.getByTestId("ref-switcher"));
    const body = within(canvasElement.ownerDocument.body);
    await body.findByRole("option", { name: /release/ });
    await userEvent.type(body.getByPlaceholderText("Find a branch or pull request…"), "nope");
    await expect(await body.findByText("No branch or pull request by that name.")).toBeVisible();
  },
};

/** A push's index landed while the page was open: what changed, and "Refresh view". */
export const NewCommits: Story = {
  render: () => (
    <NewCommitsNotice
      label="New commits indexed on main · 1 file changed"
      sha={PUSHED_SHA}
      files={PUSHED_FILES}
      action="Refresh view"
      onAction={fn()}
      onDismiss={fn()}
    />
  ),
  play: async ({ canvas }) => {
    const banner = canvas.getByTestId("new-commits");
    await expect(banner).toHaveTextContent("New commits indexed on main · 1 file changed");
    await expect(banner).toHaveTextContent("c0ffee1");
    await expect(within(banner).getByRole("button", { name: "Refresh view" })).toBeVisible();
  },
};

/** A push's incremental run: ◌ with its small %, the repository still readable meanwhile. */
export const Updating: Story = {
  render: () => (
    <div className="flex flex-col gap-3">
      <RepoStatus
        showLabel
        repo={{
          status: "indexing",
          active_job: {
            id: "job-2",
            kind: "incremental",
            ref: null,
            status: "running",
            stage: "symbols",
            progress: 0.42,
            eta_s: 8,
          },
        }}
      />
      <RepoStatus
        repo={{
          status: "indexing",
          active_job: {
            id: "job-3",
            kind: "incremental",
            ref: "release",
            status: "queued",
            stage: null,
            progress: 0,
            eta_s: null,
          },
        }}
      />
    </div>
  ),
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Updating 42%")).toBeVisible();
    await expect(canvas.getAllByTestId("update-dot")).toHaveLength(2);
    await expect(canvas.getByText("Update release queued")).toBeInTheDocument();
  },
};
