import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { http } from "msw";
import { expect, userEvent, within } from "storybook/test";

import { Gallery as GalleryView } from "@/components/explore/gallery";
import { RepoPage } from "@/components/explore/repo-page";
import { MOCK_PUBLIC_REPOS, mockPublicMaps, mockPublicPage } from "@/mocks/explore";
import { handlers } from "@/mocks/handlers";
import { apiError } from "@/mocks/handlers/errors";

const OWNER = "fastapi";
const NAME = "full-stack-fastapi-template";
const page = mockPublicPage(OWNER, NAME);
const maps = mockPublicMaps(OWNER, NAME)?.maps ?? [];
if (page === null) throw new Error("the mock gallery has its template");

const meta = {
  title: "Explore/Public pages",
  parameters: {
    layout: "fullscreen",
    nextjs: { appDirectory: true, navigation: { pathname: "/explore" } },
    msw: handlers,
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

/** The gallery: each repository with what it does (or that it isn't summarized), its
 * languages, its size and the commit it was verified at. */
export const Gallery: Story = {
  render: () => <GalleryView repos={MOCK_PUBLIC_REPOS} />,
  play: async ({ canvas }) => {
    const cards = within(canvas.getByRole("list", { name: "Repositories" })).getAllByRole("link");
    await expect(cards).toHaveLength(2);
    await expect(cards[0]).toHaveAttribute("href", `/explore/${OWNER}/${NAME}`);
    await expect(cards[0]).toHaveTextContent("Verified at b1125d8");
    await expect(cards[1]).toHaveTextContent("Not summarized yet.");
    await expect(canvas.getByRole("link", { name: "Index your own repository" })).toBeVisible();
  },
};

export const Empty: Story = {
  render: () => <GalleryView repos={[]} />,
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Nothing is published here yet.")).toBeVisible();
  },
};

/** A repository's page: its symbols open their lines on GitHub at the verified commit; a
 * question asked is answered, its citations GitHub links too. */
export const Repository: Story = {
  render: () => <RepoPage page={page} maps={maps} human={{ kind: "mock" }} />,
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("heading", { level: 1 })).toHaveTextContent(`${OWNER}/${NAME}`);
    const chip = canvas.getAllByTestId("source-chip")[0];
    await expect(chip).toHaveTextContent("create_item");
    await expect(chip).toHaveAttribute(
      "href",
      `https://github.com/${OWNER}/${NAME}/blob/${page.repo.sha}/backend/app/api/routes/items.py#L61-L72`,
    );
    await expect(canvas.getByTestId("verified-sha")).toHaveTextContent("b1125d8");
    await userEvent.type(canvas.getByLabelText("Your question"), "Where are items saved?{Enter}");
    const thread = await canvas.findByTestId("explore-thread");
    const cited = await within(thread).findAllByTestId("citation-chip", {}, { timeout: 5000 });
    await expect(cited[0]).toHaveAttribute(
      "href",
      expect.stringMatching(new RegExp(`^https://github.com/${OWNER}/${NAME}/blob/`)),
    );
  },
};

/** A question the API refuses (past the day's, here) says why. */
export const Refused: Story = {
  render: () => <RepoPage page={page} maps={maps} human={{ kind: "mock" }} />,
  parameters: {
    msw: [
      http.post("/api/v1/public/repos/:owner/:name/ask", () =>
        apiError(429, "quota_exceeded", "You've reached today's 20 questions on public pages."),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: page.overview.questions[0] ?? "" }));
    await expect(await canvas.findByRole("alert")).toHaveTextContent(
      "You've reached today's 20 questions on public pages.",
    );
  },
};

/** No Turnstile key here: no questions, and the page says so. */
export const Closed: Story = {
  render: () => <RepoPage page={page} maps={[]} human={{ kind: "off" }} />,
  play: async ({ canvas }) => {
    await expect(canvas.getByText(/aren't open here yet/)).toBeVisible();
    await expect(canvas.queryByLabelText("Your question")).toBeNull();
  },
};
