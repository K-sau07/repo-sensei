import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { http, HttpResponse } from "msw";
import { expect, userEvent } from "storybook/test";

import { ConnectRepo } from "@/components/repos/connect-repo";
import { apiError, handlers } from "@/mocks/handlers";
import { estimateFor } from "@/mocks/handlers/repos";

const meta = {
  title: "Repos/Connect a repository",
  component: ConnectRepo,
  args: { org: "acme" },
  parameters: {
    nextjs: {
      appDirectory: true,
      navigation: { pathname: "/acme/repos/new", segments: [["org", "acme"]] },
    },
  },
} satisfies Meta<typeof ConnectRepo>;

export default meta;
type Story = StoryObj<typeof meta>;

export const FromGitHub: Story = {
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("acme-gh/web")).toBeVisible();
    await expect(await canvas.findByText("Connected")).toBeVisible();
    // The installation (for its "manage" link) is a separate request.
    await expect(await canvas.findByRole("link", { name: /Add more on GitHub/ })).toBeVisible();
  },
};

export const NoInstallationYet: Story = {
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/github/installations", () => HttpResponse.json([])),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("Install the GitHub App first")).toBeVisible();
    await expect(canvas.getByRole("link", { name: /Install the GitHub App/ })).toHaveAttribute(
      "href",
      "/api/v1/github/install?org=acme&from=repos",
    );
  },
};

async function paste(canvas: Parameters<NonNullable<Story["play"]>>[0]["canvas"], url: string) {
  await userEvent.click(await canvas.findByRole("tab", { name: "Public URL" }));
  await userEvent.type(canvas.getByLabelText("Public repository URL"), url);
}

export const PublicEstimate: Story = {
  play: async ({ canvas }) => {
    await paste(canvas, "https://github.com/langflow-ai/langflow");
    const card = await canvas.findByRole("region", { name: "Estimate for langflow-ai/langflow" });
    await expect(card).toHaveTextContent("Indexing time");
    await expect(canvas.getByRole("button", { name: "Connect and index" })).toBeEnabled();
  },
};

export const UnpricedEstimate: Story = {
  parameters: {
    msw: [
      http.post("*/api/v1/orgs/:org/repos/estimate", () =>
        HttpResponse.json({
          ...estimateFor("langflow-ai/langflow", false, { Python: 900_000 }),
          unpriced: ["claude-unknown-1"],
        }),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await paste(canvas, "https://github.com/langflow-ai/langflow");
    const card = await canvas.findByRole("region", { name: "Estimate for langflow-ai/langflow" });
    const cost = await canvas.findByText("Unknown");
    await expect(card).toContainElement(cost);
    await expect(cost).toHaveAccessibleDescription(
      "No price for claude-unknown-1 on this server, so the cost is unknown.",
    );
  },
};

export const NotPublic: Story = {
  play: async ({ canvas }) => {
    await paste(canvas, "https://github.com/acme-gh/secret");
    await expect(await canvas.findByText(/isn't a public GitHub repository/)).toBeVisible();
    await expect(canvas.getByRole("button", { name: "Connect and index" })).toBeDisabled();
  },
};

export const AlreadyConnected: Story = {
  parameters: {
    msw: [
      http.post("*/api/v1/orgs/:org/repos/import", () =>
        apiError(409, "repo_already_connected", "langflow-ai/langflow is already connected.", {
          slug: "langflow",
        }),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await paste(canvas, "https://github.com/langflow-ai/langflow");
    await canvas.findByRole("region", { name: /Estimate for/ });
    await userEvent.click(canvas.getByRole("button", { name: "Connect and index" }));
    await expect(await canvas.findByText(/is already connected/)).toBeVisible();
    await expect(canvas.getByRole("link", { name: "Open it" })).toHaveAttribute(
      "href",
      "/acme/langflow",
    );
  },
};

export const CostCapExceeded: Story = {
  parameters: {
    msw: [
      http.post("*/api/v1/orgs/:org/repos/import", () =>
        apiError(
          429,
          "cost_cap_reached",
          "Indexing this would go over the organization's daily spend limit.",
        ),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await paste(canvas, "https://github.com/langflow-ai/langflow");
    await canvas.findByRole("region", { name: /Estimate for/ });
    await userEvent.click(canvas.getByRole("button", { name: "Connect and index" }));
    await expect(await canvas.findByText(/An admin can raise the limit/)).toBeVisible();
  },
};
