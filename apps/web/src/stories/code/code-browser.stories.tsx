import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { delay, http, HttpResponse } from "msw";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { CodeBrowser, CodePathView } from "@/components/code/code-browser";
import { FileTree } from "@/components/code/file-tree";
import type { TreeEntry, TreeListing } from "@/lib/api/types";
import { BROWSE_COMMIT, longFileContent } from "@/mocks/browse";
import { apiError, handlers } from "@/mocks/handlers";

const navigation = (path: string[]) => ({
  appDirectory: true,
  navigation: {
    pathname: ["/acme/api/code", ...path].join("/"),
    segments: [
      ["org", "acme"],
      ["repo", "api"],
      ["path", path],
    ],
  },
});

function Browser() {
  return (
    <div className="flex h-[640px] flex-col overflow-hidden rounded-xl border border-line">
      <CodeBrowser>
        <CodePathView />
      </CodeBrowser>
    </div>
  );
}

const meta = {
  title: "Code/Code browser",
  component: Browser,
  parameters: { nextjs: navigation([]), msw: handlers },
} satisfies Meta<typeof Browser>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The repository's root: the tree beside the root's listing. */
export const Root: Story = {
  play: async ({ canvas }) => {
    const tree = await canvas.findByRole("tree", { name: "Repository files" });
    await expect(within(tree).getAllByRole("treeitem")[0]).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    await expect(await canvas.findByRole("table", { name: "Contents of api" })).toBeVisible();
    await expect(canvas.getByRole("list", { name: "Layers" })).toHaveTextContent("Service");
  },
};

/** A Python file, revealed in the tree, with lines 14–16 lit from `?L=`. */
export const PythonFile: Story = {
  parameters: { nextjs: navigation(["backend", "app", "main.py"]) },
  decorators: [
    (Story) => (
      <NuqsTestingAdapter searchParams="?L=14-16">
        <Story />
      </NuqsTestingAdapter>
    ),
  ],
  play: async ({ canvas, canvasElement }) => {
    await expect(await canvas.findByRole("heading", { name: "main.py" })).toBeVisible();
    await expect(await canvas.findByText(/def custom_generate_unique_id/)).toBeVisible();
    const tree = canvas.getByRole("tree", { name: "Repository files" });
    await waitFor(async () => {
      await expect(within(tree).getByRole("treeitem", { name: /^main\.py/ })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });
    await expect(canvas.getByText("36 lines")).toBeVisible();
    await expect(canvas.getByText("Lines 14–16")).toBeVisible();
    await waitFor(async () => {
      await expect(canvasElement.querySelectorAll(".cm-range-line")).toHaveLength(3);
    });
    await expect(canvas.getByRole("link", { name: /Open on GitHub/ })).toHaveAttribute(
      "href",
      `https://github.com/acme-gh/api/blob/${BROWSE_COMMIT}/backend/app/main.py#L14-L16`,
    );
  },
};

/** An image: the API answers 415 with its metadata. */
export const BinaryFile: Story = {
  parameters: { nextjs: navigation(["img", "login.png"]) },
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByRole("heading", { name: "This file isn't text" }),
    ).toBeVisible();
    // The API's own download link, not GitHub's (which a private repository would refuse).
    await expect(canvas.getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      expect.stringContaining("X-Amz-Expires") as string,
    );
  },
};

/** Over 2 MB: 413, metadata and a download link. */
export const LargeFile: Story = {
  parameters: {
    nextjs: navigation(["release-notes.md"]),
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/file", () =>
        apiError(
          413,
          "file_too_large",
          "This file is too large to show here; download it instead.",
          {
            commit_sha: BROWSE_COMMIT,
            path: "release-notes.md",
            language: "markdown",
            role: "docs",
            size: 3_400_000,
            loc: null,
            flags: ["too_large"],
            sha: "0".repeat(40),
            download_url: "https://storage.example.com/artifacts/release-notes.md",
          },
        ),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByRole("heading", { name: "This file is too large to show" }),
    ).toBeVisible();
    await expect(canvas.getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      "https://storage.example.com/artifacts/release-notes.md",
    );
  },
};

/** 10,000 lines: the viewer renders only what is on screen. */
export const LongFile: Story = {
  parameters: {
    nextjs: navigation(["backend", "app", "utils.py"]),
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/file", () =>
        HttpResponse.json(
          {
            path: "backend/app/utils.py",
            language: "python",
            role: "service",
            size: longFileContent().length,
            loc: 10_000,
            flags: [],
            sha: "0".repeat(40),
            encoding: "utf-8",
            content: longFileContent(),
          },
          { headers: { "X-Commit-Sha": BROWSE_COMMIT } },
        ),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas, canvasElement }) => {
    await expect(await canvas.findByText("10,000 lines")).toBeVisible();
    await expect(await canvas.findByText(/def handler_0001/)).toBeVisible();
    await expect(canvasElement.querySelectorAll(".cm-line").length).toBeLessThan(200);
  },
};

/** The first index hasn't finished: nothing to browse yet. */
export const NotIndexed: Story = {
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/tree", () =>
        apiError(404, "repo_not_indexed", "This repository hasn't been indexed yet."),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("Not indexed yet")).toBeVisible();
  },
};

/** The listing failed (after retries): say so, with the request ID. */
export const Failure: Story = {
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/tree", () =>
        apiError(503, "service_unavailable", "The API is restarting."),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByText("Couldn't load the code", undefined, { timeout: 15_000 }),
    ).toBeVisible();
    await expect(canvas.getByText("mock-request-id")).toBeVisible();
  },
};

// ----------------------------------------------------------------- the tree alone

const COUNT = 100_000;

/** A repository with 100,000 files in one directory, listed by a mock of `GET /tree`. */
const hugeTree = http.get("*/api/v1/orgs/:org/repos/:repo/tree", ({ request }) => {
  const path = new URL(request.url).searchParams.get("path") ?? "";
  const entries: TreeEntry[] =
    path === ""
      ? [{ name: "generated", path: "generated", type: "dir", child_count: COUNT }]
      : Array.from({ length: COUNT }, (_, index) => {
          const name = `file_${String(index + 1).padStart(6, "0")}.ts`;
          return { name, path: `generated/${name}`, type: "file", role: "ui", loc: 3, size: 120 };
        });
  return HttpResponse.json<TreeListing>(
    { path, entries },
    { headers: { "X-Commit-Sha": BROWSE_COMMIT } },
  );
});

function TreeAlone({ defaultExpanded }: { defaultExpanded?: string[] }) {
  return (
    <div className="flex h-[560px] w-80 flex-col rounded-xl border border-line">
      <FileTree
        className="min-h-0 flex-1"
        current=""
        onOpenFile={() => undefined}
        {...(defaultExpanded ? { defaultExpanded } : {})}
      />
    </div>
  );
}

/** The 100k-file smoke test: only the rows on screen exist, and End reaches the last one. */
export const HundredThousandFiles: StoryObj<typeof TreeAlone> = {
  render: () => <TreeAlone defaultExpanded={["generated"]} />,
  parameters: { nextjs: navigation([]), msw: [hugeTree, ...handlers] },
  play: async ({ canvas }) => {
    const tree = await canvas.findByRole("tree", { name: "Repository files" });
    const first = await within(tree).findByRole("treeitem", { name: /^file_000001\.ts/ });
    await expect(first).toHaveAttribute("aria-setsize", String(COUNT));
    await expect(within(tree).getAllByRole("treeitem").length).toBeLessThan(80);
    tree.focus();
    await userEvent.keyboard("{End}");
    const last = await within(tree).findByRole("treeitem", { name: /^file_100000\.ts/ });
    await expect(tree).toHaveAttribute("aria-activedescendant", last.id);
    await expect(last).toHaveAttribute("aria-posinset", String(COUNT));
    await expect(within(tree).getAllByRole("treeitem").length).toBeLessThan(80);
  },
};

/** A folder whose listing fails: marked on its row, and Enter tries again. */
export const FolderFails: StoryObj<typeof TreeAlone> = {
  render: () => <TreeAlone />,
  parameters: {
    nextjs: navigation([]),
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/tree", ({ request }) =>
        new URL(request.url).searchParams.get("path") === "backend"
          ? apiError(404, "path_not_found", "No such directory in this snapshot.")
          : undefined,
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    const backend = await canvas.findByRole("treeitem", { name: /^backend/ });
    await userEvent.click(backend);
    await expect(
      await canvas.findByRole("treeitem", { name: /couldn't load; press Enter to retry/ }),
    ).toHaveAttribute("aria-expanded", "true");
  },
};

/** Still loading the root. */
export const Loading: StoryObj<typeof TreeAlone> = {
  render: () => <TreeAlone />,
  parameters: {
    nextjs: navigation([]),
    msw: [
      http.get("*/api/v1/orgs/:org/repos/:repo/tree", async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("status", { name: "Loading files" })).toBeVisible();
  },
};
