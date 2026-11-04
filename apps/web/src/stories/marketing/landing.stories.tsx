import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, userEvent } from "storybook/test";

import { LiveDemo } from "@/components/marketing/landing/live-demo";
import { RepoInput } from "@/components/marketing/landing/repo-input";
import {
  Closing,
  Editors,
  Features,
  ProofStrip,
  Safety,
} from "@/components/marketing/landing/sections";
import { mockPublicPage } from "@/mocks/explore";
import { handlers } from "@/mocks/handlers";

const page = mockPublicPage("fastapi", "full-stack-fastapi-template");

const meta = {
  title: "Marketing/Landing",
  parameters: {
    layout: "padded",
    nextjs: { appDirectory: true, navigation: { pathname: "/" } },
    msw: handlers,
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

/** The one input: anything that isn't a GitHub repository's URL is said so at once. */
export const Input: Story = {
  render: () => <RepoInput />,
  play: async ({ canvas }) => {
    await userEvent.type(canvas.getByLabelText("A public GitHub repository's URL"), "not a url");
    await userEvent.click(canvas.getByRole("button", { name: "Understand it" }));
    await expect(await canvas.findByRole("alert")).toHaveTextContent(
      "isn't a GitHub repository's URL",
    );
  },
};

/** The demo on a published repository, its suggested questions ready to ask. */
export const Demo: Story = {
  render: () => <LiveDemo page={page} human={{ kind: "mock" }} />,
  play: async ({ canvas }) => {
    await expect(
      await canvas.findByRole("button", { name: "How does the API routes work?" }),
    ).toBeEnabled();
  },
};

/** Nothing published: the demo points to Explore instead. */
export const NoDemo: Story = {
  render: () => <LiveDemo page={null} human={{ kind: "off" }} />,
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("link", { name: "Explore repositories" })).toHaveAttribute(
      "href",
      "/explore",
    );
  },
};

/** What the product keeps to, does, keeps safe, works in, and where to start. */
export const Sections: Story = {
  render: () => (
    <div className="flex flex-col gap-12">
      <ProofStrip />
      <Features />
      <Safety />
      <Editors />
      <Closing />
    </div>
  ),
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("heading", { name: "Your code stays yours" })).toBeVisible();
    await expect(canvas.getByText("Claude Code")).toBeVisible();
  },
};
