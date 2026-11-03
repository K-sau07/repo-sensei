import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { getRouter } from "@storybook/nextjs-vite/navigation.mock";
import { http, HttpResponse } from "msw";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { InboxPage } from "@/components/inbox/inbox-page";
import type { InboxPage as Page } from "@/lib/api/inbox";
import { resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";

const nextjs = {
  appDirectory: true,
  navigation: { pathname: "/acme/inbox", segments: [["org", "acme"]] },
};

const meta = {
  title: "Inbox/Inbox",
  component: InboxPage,
  parameters: { nextjs, msw: handlers },
  // The filters live in the URL: kept, as a browser would.
  decorators: [
    (Story) => (
      <NuqsTestingAdapter hasMemory>
        <Story />
      </NuqsTestingAdapter>
    ),
  ],
  beforeEach: () => {
    resetMockDb(); // the stories mark items read
  },
} satisfies Meta<typeof InboxPage>;

export default meta;
type Story = StoryObj<typeof meta>;

const F001 = "/acme/api/fixes/0199f000-0000-7000-8000-00000000f001";

/** What's unread, newest first: two plans waiting (with their risk), a fix that stopped.
 * `j`/`k` move, `e` marks the focused item done (and moves on), `a` opens an approval. */
export const Triage: Story = {
  play: async ({ canvas }) => {
    const items = await canvas.findAllByTestId("inbox-item");
    await expect(items.map((item) => item.dataset.kind)).toEqual([
      "approval_needed",
      "approval_needed",
      "fix_failed",
    ]);
    await expect(canvas.getByTestId("inbox-unread")).toHaveTextContent("3 unread");
    const [first, second] = items;
    if (!first || !second) throw new Error("the inbox shows fewer items than seeded");
    await expect(within(first).getByText("High risk")).toBeInTheDocument();
    await expect(within(second).getByText("Low risk")).toBeInTheDocument();
    // j: the first; e: done, the next one focused (the first leaves the unread view).
    await userEvent.keyboard("j");
    await expect(
      within(first).getByRole("link", { name: /Refunds are applied twice/ }),
    ).toHaveFocus();
    // From its Done button too: the item's.
    within(first).getByRole("button", { name: /^Done/ }).focus();
    await userEvent.keyboard("j");
    await expect(within(second).getByRole("link", { name: /Items page crashes/ })).toHaveFocus();
    await userEvent.keyboard("k");
    await userEvent.keyboard("e");
    await waitFor(() => expect(canvas.getByTestId("inbox-unread")).toHaveTextContent("2 unread"));
    await waitFor(() => expect(canvas.getAllByTestId("inbox-item")).toHaveLength(2));
    const approval = canvas.getByRole("link", { name: /Items page crashes/ });
    await expect(approval).toHaveFocus();
    // a: the approval card, the item read.
    await userEvent.keyboard("a");
    await expect(getRouter().push).toHaveBeenCalledWith(F001);
    await waitFor(() => expect(canvas.getByTestId("inbox-unread")).toHaveTextContent("1 unread"));
    // a on what isn't an approval does nothing; k goes back up.
    await userEvent.keyboard("j");
    await expect(canvas.getByRole("link", { name: /Login redirects/ })).toHaveFocus();
    await userEvent.keyboard("a");
    await expect(getRouter().push).toHaveBeenCalledTimes(1);
  },
};

/** Everything, read or not, one group at a time; an item with nowhere to go is read by
 * opening it. */
export const AllByGroup: Story = {
  play: async ({ canvas }) => {
    await canvas.findAllByTestId("inbox-item");
    await userEvent.click(canvas.getByRole("button", { name: "All" }));
    await waitFor(() => expect(canvas.getAllByTestId("inbox-item")).toHaveLength(6));
    // A group's kinds are asked of the API: every page it sends is theirs.
    const kinds = () => canvas.getAllByTestId("inbox-item").map((item) => item.dataset.kind);
    await userEvent.click(canvas.getByRole("button", { name: "Failures" }));
    await waitFor(() => expect(kinds()).toEqual(["fix_failed", "cost_cap"]));
    await userEvent.click(canvas.getByRole("button", { name: "Finished" }));
    await waitFor(() => expect(kinds()).toEqual(["pr_opened", "index_done"]));
    await userEvent.click(canvas.getByRole("button", { name: "Everything" }));
    await userEvent.click(canvas.getByRole("button", { name: "Mark all read" }));
    await waitFor(() =>
      expect(canvas.getByTestId("inbox-unread")).toHaveTextContent("Nothing unread"),
    );
    await expect(canvas.getByRole("button", { name: "Mark all read" })).toBeDisabled();
  },
};

/** Nothing unread. */
export const CaughtUp: Story = {
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/inbox", () =>
        HttpResponse.json<Page>({ items: [], unread: 0, next_cursor: null }),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("You're all caught up")).toBeVisible();
  },
};
