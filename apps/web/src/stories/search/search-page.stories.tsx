import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http } from "msw";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { SearchPage } from "@/components/search/search-page";
import { apiError, handlers } from "@/mocks/handlers";
import { SEARCH_SCENARIO_KEY } from "@/mocks/handlers/search";

const navigation = {
  appDirectory: true,
  navigation: {
    pathname: "/acme/api/search",
    segments: [
      ["org", "acme"],
      ["repo", "api"],
    ],
  },
};

/** The page's URL query, as the testing adapter holds it. */
function withQuery(search: string) {
  return function WithQuery(Story: () => React.ReactNode) {
    return (
      <NuqsTestingAdapter searchParams={search}>
        <Story />
      </NuqsTestingAdapter>
    );
  };
}

/** A mock search situation (mocks/handlers/search.ts) for one story. */
const scenario = (value: string) => () => {
  localStorage.setItem(SEARCH_SCENARIO_KEY, value);
  return () => {
    localStorage.removeItem(SEARCH_SCENARIO_KEY);
  };
};

const lane = (root: HTMLElement, name: string): Promise<HTMLElement> =>
  within(root).findByRole("region", { name: new RegExp(`^${name}, `) });

const meta = {
  title: "Search/Search page",
  component: SearchPage,
  parameters: { nextjs: navigation, msw: handlers, bare: true },
  decorators: [
    (Story) => (
      <div className="flex min-h-[640px] flex-col">
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof SearchPage>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A name: definitions in the API and DB lanes, uses in the UI; empty lanes narrow. */
export const ResultsAcrossLanes: Story = {
  decorators: [withQuery("?q=create_user")],
  play: async ({ canvas, canvasElement }) => {
    const api = await lane(canvasElement, "API");
    const first = (await within(api).findAllByRole("link"))[0];
    await expect(first).toHaveAccessibleName(
      "create_user function backend/app/api/routes/users.py:54–78",
    );
    await expect(first).toHaveAttribute(
      "href",
      "/acme/api/code/backend/app/api/routes/users.py?L=54-78",
    );
    await expect(within(api).getAllByRole("list", { name: "Why it matched" })[0]).toHaveTextContent(
      /symbol/,
    );
    // A match is a <mark>, never markup from the response.
    await expect(api.querySelector("mark")?.textContent).toBe("create_user");
    await expect(await lane(canvasElement, "Service")).toHaveTextContent(
      "No results in this layer.",
    );
    await expect(canvas.getByRole("button", { name: /^Read as symbol/ })).toBeVisible();
    await expect(canvas.getByRole("navigation", { name: "Pivot to exact" })).toHaveTextContent(
      "regex",
    );
    // Announced politely, once.
    const announced = await canvas.findByText(/^\d+ results in 3 lanes$/);
    await expect(announced).toHaveAttribute("aria-live", "polite");
  },
};

/** Embeddings pending: keyword and symbol matches only, and the banner says why. */
export const Partial: Story = {
  decorators: [withQuery("?q=how+are+passwords+hashed")],
  beforeEach: scenario("partial"),
  play: async ({ canvas, canvasElement }) => {
    await expect(await canvas.findByText("Keyword and symbol matches only.")).toBeVisible();
    await expect(canvas.getByText(/embeddings aren.t ready yet/)).toBeVisible();
    await lane(canvasElement, "Service");
    await expect(canvas.queryAllByText("semantic")).toHaveLength(0);
  },
};

/** Nothing matched: ways to widen the search, or to ask. */
export const Empty: Story = {
  decorators: [withQuery("?q=zzqxv_nothing_here&layer=db")],
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("heading", { name: "No results" })).toBeVisible();
    await expect(canvas.getByRole("link", { name: "Search every layer and path" })).toHaveAttribute(
      "href",
      "/acme/api/search?q=zzqxv_nothing_here",
    );
    await expect(canvas.getByRole("link", { name: "Ask instead" })).toBeVisible();
  },
};

/** The search failed: what happened, and Try again (retries are off, so it shows at once). */
export const Failed: Story = {
  decorators: [
    withQuery("?q=login"),
    (Story) => (
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <Story />
      </QueryClientProvider>
    ),
  ],
  beforeEach: scenario("error"),
  play: async ({ canvas }) => {
    const alert = await canvas.findByRole("alert");
    await expect(alert).toHaveTextContent("Couldn't search");
    await expect(within(alert).getByRole("button", { name: "Try again" })).toBeVisible();
    await expect(alert).toHaveTextContent("mock-request-id");
  },
};

/** Too many searches: when to try again, counted down. */
export const RateLimited: Story = {
  decorators: [withQuery("?q=login")],
  beforeEach: scenario("rate-limited"),
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByRole("heading", { name: "Too many searches for now" }),
    ).toBeVisible();
    await expect(canvas.getByRole("alert")).toHaveTextContent(
      /You can search again in \d+ seconds/,
    );
    await expect(canvas.getByRole("button", { name: "Try again" })).toBeDisabled();
  },
};

/** A first run before its keyword index: the API answers 409, the page says when. */
export const NotIndexed: Story = {
  decorators: [withQuery("?q=login")],
  parameters: {
    msw: [
      http.post("*/api/v1/orgs/:org/repos/:repo/search", () =>
        apiError(409, "not_indexed", "Search appears once this commit's keyword index is built."),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByRole("heading", { name: "Search appears after indexing" }),
    ).toBeVisible();
    await expect(canvas.getByRole("link", { name: "Indexing" })).toBeVisible();
  },
};

/** Exact text on a minified line thousands of characters long: the card shows the match. */
export const LongSnippets: Story = {
  decorators: [withQuery("?q=%22%C2%A9%22")],
  play: async ({ canvasElement }) => {
    const ui = await lane(canvasElement, "UI");
    const marks = await waitFor(() => {
      const found = ui.querySelectorAll("mark");
      if (found.length === 0) throw new Error("no marks yet");
      return found;
    });
    await expect(marks[0]?.textContent).toBe("©");
    const line = marks[0]?.closest("code");
    await expect(line?.textContent).toMatch(/^….*…$/);
    await expect((line?.textContent ?? "").length).toBeLessThan(200);
  },
};

/** A narrow screen: one lane at a time, picked from a segmented control. */
export const Narrow: Story = {
  decorators: [
    withQuery("?q=create_user"),
    (Story) => (
      <div className="w-[390px] border-r border-line">
        <Story />
      </div>
    ),
  ],
  play: async ({ canvas, canvasElement }) => {
    const picker = await canvas.findByRole("group", { name: "Lane" });
    const ui = await lane(canvasElement, "UI");
    await waitFor(async () => {
      await expect(ui).toBeVisible();
    });
    await expect(canvasElement.querySelector('[data-lane="api"]')).not.toBeVisible();
    await userEvent.click(within(picker).getByRole("button", { name: /API/ }));
    await expect(await lane(canvasElement, "API")).toBeVisible();
    await expect(within(picker).getByRole("button", { name: /API/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  },
};
