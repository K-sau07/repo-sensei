import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { RepoSettings } from "@/components/repo/repo-settings";
import { db, OCTO, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";

const at = (repo: string) => ({
  appDirectory: true,
  navigation: {
    pathname: `/acme/${repo}/settings`,
    segments: [
      ["org", "acme"],
      ["repo", repo],
    ],
  },
});

const meta = {
  title: "Repository/Settings",
  component: RepoSettings,
  parameters: { nextjs: at("api"), msw: handlers },
  beforeEach: () => {
    resetMockDb(); // the stories change and disconnect it
  },
} satisfies Meta<typeof RepoSettings>;

export default meta;
type Story = StoryObj<typeof meta>;

const api = () => db.repos.acme?.find((r) => r.slug === "api");

/** What is indexed: a branch tracked, a pattern left out, pull requests no more; a branch name
 * the API refuses says why. Only the GitHub App's repository: no gallery here. */
export const Indexing: Story = {
  play: async ({ canvas }) => {
    const save = await canvas.findByRole("button", { name: "Save" });
    await expect(save).toBeDisabled();
    await expect(canvas.queryByText("Public gallery")).toBeNull();
    await userEvent.type(canvas.getByLabelText("A branch to track"), "staging{Enter}");
    await userEvent.type(canvas.getByLabelText("Left out of the index"), "docs/generated/**");
    await userEvent.click(canvas.getByRole("switch", { name: "Index pull requests" }));
    await userEvent.click(save);
    await waitFor(async () => {
      await expect(api()?.settings).toEqual({
        tracked_branches: ["release", "staging"],
        ignore_globs: ["docs/generated/**"],
        auto_index: true,
        index_pull_requests: false,
      });
    });
    // Saved: the form is what was saved, nothing left to save.
    await waitFor(async () => {
      await expect(canvas.getByRole("button", { name: "Save" })).toBeDisabled();
    });
    await userEvent.type(canvas.getByLabelText("A branch to track"), "bad..name{Enter}");
    await userEvent.click(canvas.getByRole("button", { name: "Save" }));
    await expect(await canvas.findByRole("alert")).toHaveTextContent("not a valid branch name");
    await userEvent.click(canvas.getByRole("button", { name: "Stop tracking bad..name" }));
    await expect(canvas.getByRole("button", { name: "Save" })).toBeDisabled();
  },
};

/** The explore org's repository imported by URL: its admins show it in the public gallery. */
export const Gallery: Story = {
  parameters: { nextjs: at("tiny") },
  beforeEach: () => {
    const base = db.repos.acme?.[0];
    if (!base) throw new Error("the mock org has its repository");
    db.repos.acme?.push({
      ...base,
      id: "0199f000-0000-7000-8000-0000000000d9",
      slug: "tiny",
      full_name: "octo/tiny",
      owner: "octo",
      name: "tiny",
      source: "public_url",
      visibility: "public",
      publishable: true,
      published_at: null,
    });
  },
  play: async ({ canvas }) => {
    const shown = await canvas.findByRole("switch", { name: "Show it in the public gallery" });
    await expect(shown).not.toBeChecked();
    await userEvent.click(shown);
    await waitFor(async () => {
      await expect(
        canvas.getByRole("switch", { name: "Show it in the public gallery" }),
      ).toBeChecked();
    });
    await expect(canvas.getByRole("link", { name: /Its public page/ })).toHaveAttribute(
      "href",
      "/explore/octo/tiny",
    );
  },
};

/** Disconnecting asks for the repository's name first. */
export const Disconnect: Story = {
  play: async ({ canvas, canvasElement }) => {
    await userEvent.click(await canvas.findByRole("button", { name: "Disconnect acme-gh/api" }));
    const dialog = within(canvasElement.ownerDocument.body).getByRole("alertdialog");
    const go = within(dialog).getByRole("button", { name: "Disconnect" });
    await expect(go).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("The repository's name"), "acme-gh/api");
    await expect(go).toBeEnabled();
    await userEvent.click(go);
    await waitFor(async () => {
      await expect(api()).toBeUndefined();
    });
  },
};

/** A member reads the settings but changes nothing. */
export const Member: Story = {
  beforeEach: () => {
    const me = db.members.acme?.find((m) => m.user_id === OCTO);
    if (me) me.role = "member";
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText(/Only the organization's admins/)).toBeVisible();
    await expect(canvas.getByRole("switch", { name: "Index automatically" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await expect(canvas.queryByRole("button", { name: "Save" })).toBeNull();
    await expect(canvas.queryByRole("button", { name: /Disconnect/ })).toBeNull();
  },
};
