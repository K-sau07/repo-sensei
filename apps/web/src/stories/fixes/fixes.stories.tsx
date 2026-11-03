import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { http, HttpResponse } from "msw";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { FixesPage } from "@/components/fixes/fixes-page";
import { NewFixPage } from "@/components/fixes/new-fix-page";
import { db, OCTO } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";
import { meOf } from "@/mocks/handlers/me";

const navigation = (section: string) => ({
  appDirectory: true,
  navigation: {
    pathname: `/acme/api/${section}`,
    segments: [
      ["org", "acme"],
      ["repo", "api"],
    ],
  },
});

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

const LOAD_MS = 10_000;

const TRACE = `Traceback (most recent call last):
  File "/app/backend/app/api/routes/items.py", line 34, in read_items
    count = crud.count_items(session=session, owner_id=current_user.id)
  File "/app/backend/app/crud.py", line 61, in count_items
    return total // per_page
  File "/usr/lib/python3.13/site-packages/sqlalchemy/orm/session.py", line 9, in execute
ZeroDivisionError: integer division or modulo by zero`;

const meta = {
  title: "Fixes/Fixes",
  component: FixesPage,
  parameters: { nextjs: navigation("fixes"), msw: handlers },
} satisfies Meta<typeof FixesPage>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The repository's fixes: each state's tone, risk, its pull request, who asked. */
export const List: Story = {
  play: async ({ canvas }) => {
    // The list waits for the repository's index state (slower on a shared CI runner).
    const rows = await canvas.findAllByTestId("fix-row", {}, { timeout: LOAD_MS });
    await expect(rows).toHaveLength(4);
    await expect(within(rows[0] ?? document.body).getByTestId("fix-status")).toHaveTextContent(
      "Awaiting approval",
    );
    await expect(canvas.getByRole("link", { name: /Draft PR/ })).toHaveAttribute(
      "href",
      "https://github.com/acme/api/pull/51",
    );
    // Sentry's issue: from Sentry, asked for by Sentry (no person), waiting for triage.
    const sentry = within(rows[1] ?? document.body);
    await expect(sentry.getAllByText("Sentry")).toHaveLength(2);
    await expect(sentry.getByTestId("fix-status")).toHaveTextContent("Needs triage");
    await expect(within(rows[2] ?? document.body).getByText("@hubot")).toBeVisible();
    await expect(canvas.getByRole("link", { name: "New fix" })).toBeVisible();
  },
};

/** A viewer follows fixes; members start them. */
export const AsViewer: Story = {
  parameters: { msw: [asViewer, ...handlers] },
  play: async ({ canvas }) => {
    await expect(await canvas.findByTestId("viewer-note", {}, { timeout: LOAD_MS })).toBeVisible();
    await expect(canvas.queryByRole("link", { name: "New fix" })).toBeNull();
  },
};

/** A paste parsed as it is typed: the error, and the frames as chips (the repository's open the
 * code at their line; a library's isn't a link). */
export const NewFromPaste: StoryObj<typeof NewFixPage> = {
  render: () => <NewFixPage />,
  parameters: { nextjs: navigation("fixes/new") },
  play: async ({ canvas }) => {
    const box = await canvas.findByLabelText("Stack trace or what goes wrong");
    await userEvent.click(box);
    await userEvent.paste(TRACE);
    const preview = await canvas.findByTestId("trace-preview", {}, { timeout: 3000 });
    await waitFor(async () => {
      await expect(preview).toHaveTextContent("ZeroDivisionError");
    });
    const chips = within(preview).getAllByTestId("frame-chip");
    await expect(chips).toHaveLength(2);
    await expect(chips[1]).toHaveAttribute("href", "/acme/api/code/backend/app/crud.py?L=61");
    await expect(preview).toHaveTextContent("3 frames, 2 in this repository.");
    await expect(canvas.getByRole("button", { name: "Start investigation" })).toBeEnabled();
  },
};

/** A viewer can't start a fix: the page says what role it needs. */
export const NewAsViewer: StoryObj<typeof NewFixPage> = {
  render: () => <NewFixPage />,
  parameters: { nextjs: navigation("fixes/new"), msw: [asViewer, ...handlers] },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("You need the member role")).toBeVisible();
    await expect(canvas.queryByRole("tab", { name: "Paste trace" })).toBeNull();
  },
};
