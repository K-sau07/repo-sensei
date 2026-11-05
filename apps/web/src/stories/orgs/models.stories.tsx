import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { http, HttpResponse } from "msw";
import { expect, screen, userEvent, waitFor, within } from "storybook/test";

import { ModelSettings } from "@/components/orgs/model-settings";
import { db, OCTO, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";
import { meOf } from "@/mocks/handlers/me";

const meta = {
  title: "Settings/Models",
  component: ModelSettings,
  parameters: {
    nextjs: {
      appDirectory: true,
      navigation: { pathname: "/acme/settings/models", segments: [["org", "acme"]] },
    },
    msw: handlers,
  },
  beforeEach: () => {
    resetMockDb();
  },
} satisfies Meta<typeof ModelSettings>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Each step as recommended, said in words; raising one saves it at once. */
export const Models: Story = {
  play: async ({ canvas }) => {
    const answers = await canvas.findByRole("region", { name: "Answers" });
    await expect(answers).toHaveTextContent("Recommended: Sonnet 5.5, at low effort for a lookup");
    await expect(canvas.getByRole("region", { name: "Fixes and changes" })).toBeVisible();
    await expect(canvas.getByText(/Summaries made while indexing always run on Haiku 5\.5/))
      .toBeVisible(); // prettier-ignore
    const model = within(answers).getByRole("combobox", { name: "Answers: model" });
    await expect(model).toHaveTextContent("Recommended");
    await userEvent.click(model);
    await userEvent.click(await screen.findByRole("option", { name: "At least Opus 5.5" }));
    await waitFor(() => expect(model).toHaveTextContent("At least Opus 5.5"));
    await expect(db.modelChoices?.acme?.answers).toEqual({ tier: "deep", effort: "recommended" });
  },
};

/** A member is told only admins choose the models. */
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
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByText("Only the organization's admins choose its models."),
    ).toBeVisible();
  },
};
