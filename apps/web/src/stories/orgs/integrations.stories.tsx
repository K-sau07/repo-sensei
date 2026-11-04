import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { delay, http } from "msw";
import { expect, userEvent, waitFor, within } from "storybook/test";

import { GitHubInstallations } from "@/components/orgs/github-installations";
import { SentrySettings } from "@/components/orgs/sentry-settings";
import { db, resetMockDb } from "@/mocks/db";
import { handlers } from "@/mocks/handlers";

const nextjs = {
  appDirectory: true,
  navigation: { pathname: "/acme/settings/integrations", segments: [["org", "acme"]] },
};

const SLOW_MS = 400;

const meta = {
  title: "Settings/Integrations",
  parameters: { nextjs, msw: handlers },
  beforeEach: () => {
    resetMockDb();
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

/** The GitHub App's installations, each with a link to change it on GitHub, and one more. */
export const GitHub: Story = {
  render: () => <GitHubInstallations />,
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("acme-gh")).toBeVisible();
    await expect(canvas.getByText(/organization · chosen repositories/)).toBeVisible();
    await expect(canvas.getByRole("link", { name: /Change on GitHub/ })).toHaveAttribute(
      "href",
      "https://github.com/organizations/acme-gh/settings/installations/555001",
    );
    await expect(canvas.getByRole("link", { name: /Install on another account/ })).toHaveAttribute(
      "href",
      "/api/v1/github/install?org=acme&from=integrations",
    );
  },
};

/** Connecting needs the token and the secret; a project is mapped to a repository. */
export const ConnectSentry: Story = {
  render: () => <SentrySettings />,
  beforeEach: () => {
    db.sentry = { acme: null };
  },
  play: async ({ canvas, canvasElement }) => {
    const connect = await canvas.findByRole("button", { name: "Connect Sentry" });
    await userEvent.type(canvas.getByLabelText("Sentry organization"), "acme-errors");
    await userEvent.type(canvas.getByLabelText("Token"), "sntrys_token_123");
    await expect(connect).toBeDisabled(); // no secret yet
    await userEvent.type(canvas.getByLabelText("Client secret"), "secret_456789");
    await userEvent.click(canvas.getByRole("button", { name: "Add a project" }));
    await userEvent.type(canvas.getByLabelText("Sentry project 1"), "backend");
    await userEvent.click(canvas.getByRole("combobox", { name: "Repository for project 1" }));
    await userEvent.click(
      await within(canvasElement.ownerDocument.body).findByRole("option", { name: "acme-gh/api" }),
    );
    await userEvent.click(connect);
    await waitFor(async () => {
      await expect(db.sentry?.acme?.projects).toEqual({ backend: "api" });
    });
    await expect(await canvas.findByTestId("sentry-connected")).toHaveTextContent("acme-errors");
  },
};

/** Connected: changed without its secrets (kept), then disconnected. Sentry's settings come
 * back slowly, as on a busy machine: the confirmation opened before they arrive stays open. */
export const ChangeSentry: Story = {
  render: () => <SentrySettings />,
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/integrations/sentry", async () => {
        await delay(SLOW_MS); // then on to the mock's own answer
      }),
      ...handlers,
    ],
  },
  play: async ({ canvas, canvasElement }) => {
    await expect(await canvas.findByTestId("sentry-connected")).toHaveTextContent("acme");
    const events = canvas.getByLabelText("at");
    await userEvent.clear(events);
    await userEvent.type(events, "25");
    await userEvent.click(canvas.getByRole("button", { name: "Save" }));
    await waitFor(async () => {
      await expect(db.sentry?.acme?.min_events).toBe(25);
    });
    const disconnect = await canvas.findByRole("button", { name: "Disconnect Sentry" });
    await waitFor(async () => {
      await expect(disconnect).toBeEnabled(); // not while the save is still finishing
    });
    await userEvent.click(disconnect);
    const body = within(canvasElement.ownerDocument.body);
    await body.findByRole("alertdialog");
    await new Promise((resolve) => setTimeout(resolve, SLOW_MS * 2)); // the settings arrive
    const dialog = body.getByRole("alertdialog"); // still open
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    await expect(await canvas.findByRole("button", { name: "Connect Sentry" })).toBeVisible();
  },
};
