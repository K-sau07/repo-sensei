import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { http, HttpResponse } from "msw";
import { expect, within } from "storybook/test";

import { InsightsPage } from "@/components/insights/insights-page";
import { db, OCTO, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";
import { insights } from "@/mocks/handlers/insights";
import { meOf } from "@/mocks/handlers/me";

const LOAD_MS = 10_000;

const meta = {
  title: "Insights/Insights",
  component: InsightsPage,
  parameters: {
    nextjs: {
      appDirectory: true,
      navigation: { pathname: "/acme/insights", segments: [["org", "acme"]] },
    },
    msw: handlers,
  },
  beforeEach: () => {
    resetMockDb();
  },
} satisfies Meta<typeof InsightsPage>;

export default meta;
type Story = StoryObj<typeof meta>;

/** An admin's: the month's questions, how answers went, by repository with spend, the modules
 * answers cite (a treemap and a table) and each week. */
export const Insights: Story = {
  play: async ({ canvas }) => {
    const questions = await canvas.findByRole(
      "region",
      { name: "Questions" },
      { timeout: LOAD_MS },
    );
    await expect(questions).toHaveTextContent(/\d+/);
    await expect(canvas.getByRole("region", { name: "Rated helpful" })).toHaveTextContent("86%");
    await expect(canvas.getByRole("link", { name: "acme-gh/api" })).toHaveAttribute(
      "href",
      "/acme/api/ask",
    );
    await expect(canvas.getByText(/Wrong file/)).toBeVisible();
    await expect(
      canvas.getByRole("img", { name: /Most cited modules as a treemap/ }),
    ).toBeVisible();
    const weeks = canvas.getByRole("table", { name: "Each week" });
    await expect(within(weeks).getAllByRole("row").length).toBeGreaterThan(4);
    await expect(canvas.getByText(/ · \$/)).toBeVisible(); // spend, for admins
  },
};

/** A member reads the same counts, without what they cost. */
export const AsMember: Story = {
  parameters: {
    msw: [
      http.get("*/api/v1/me", () => {
        const octo = db.users.find((u) => u.id === OCTO);
        if (!octo) throw new Error("mock seed is missing Octo");
        const me = meOf(octo);
        return HttpResponse.json({
          ...me,
          orgs: me.orgs.map((o) => (o.slug === "acme" ? { ...o, role: "member" as const } : o)),
        });
      }),
      http.get("*/api/v1/orgs/:org/insights", () => HttpResponse.json(insights("acme", false))),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await canvas.findByRole("region", { name: "Questions" }, { timeout: LOAD_MS });
    await expect(canvas.queryByText(/ · \$/)).toBeNull();
  },
};
