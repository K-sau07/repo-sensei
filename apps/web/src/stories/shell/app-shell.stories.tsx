import type { Decorator, Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, screen, userEvent, waitFor } from "storybook/test";

import { ComingSoon } from "@/components/coming-soon";
import { AppShell } from "@/components/shell/app-shell";
import { applyShellLayout, useShellStore } from "@/components/shell/shell-store";

function ShellPreview() {
  return (
    <AppShell>
      <ComingSoon title="Workspace" />
    </AppShell>
  );
}

function withLayout(layout: { collapsed?: boolean; density?: "compact" }): Decorator {
  return function WithLayout(Story) {
    const sidebarCollapsed = layout.collapsed === true;
    applyShellLayout({ sidebarWidth: 240, sidebarCollapsed, railOpen: false });
    useShellStore.setState({ sidebarCollapsed, sidebarWidth: 240, railOpen: false });
    document.documentElement.dataset.density = layout.density ?? "comfortable";
    return <Story />;
  };
}

const meta = {
  title: "Shell/App shell",
  component: ShellPreview,
  parameters: {
    bare: true,
    nextjs: {
      appDirectory: true,
      navigation: { pathname: "/acme", segments: [["org", "acme"]] },
    },
  },
} satisfies Meta<typeof ShellPreview>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Expanded: Story = {
  decorators: [withLayout({})],
  play: async ({ canvas }) => {
    // The user and org come from the mock API.
    await expect(
      await canvas.findByRole("button", { name: "Acme (switch organization)" }),
    ).toBeVisible();
    await expect(canvas.getByRole("link", { name: "Home" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  },
};

export const Collapsed: Story = {
  decorators: [withLayout({ collapsed: true })],
  play: async ({ canvas }) => {
    const sidebar = await canvas.findByRole("complementary", { name: "Sidebar" });
    await waitFor(() => expect(sidebar.getBoundingClientRect().width).toBe(56));
    // Icon-only links keep their names for screen readers.
    await expect(canvas.getByRole("link", { name: "Inbox" })).toBeInTheDocument();
  },
};

export const Light: Story = { decorators: [withLayout({})], globals: { theme: "light" } };

export const Compact: Story = { decorators: [withLayout({ density: "compact" })] };

export const CommandPaletteOpen: Story = {
  decorators: [withLayout({})],
  play: async ({ canvas }) => {
    await userEvent.click(await canvas.findByRole("button", { name: /Search or ask/ }));
    await expect(await screen.findByRole("dialog", { name: "Command palette" })).toBeVisible();
  },
};
