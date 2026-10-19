import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect } from "storybook/test";

import { ConstellationBackdrop } from "@/components/auth/constellation-backdrop";
import { LoginPanel } from "@/components/auth/login-panel";
import { ProfileView } from "@/components/settings/profile-view";
import { SessionsView } from "@/components/settings/sessions-view";

function LoginScreen() {
  return (
    <div className="relative flex min-h-[640px] items-center justify-center overflow-hidden p-6">
      <ConstellationBackdrop />
      <div className="relative w-full max-w-sm rounded-xl border border-line bg-surface-1/90 p-8 shadow-dialog">
        <LoginPanel />
      </div>
    </div>
  );
}

const meta = {
  title: "Auth/Screens",
  parameters: { nextjs: { appDirectory: true } },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const Login: Story = {
  render: () => <LoginScreen />,
  parameters: { nextjs: { appDirectory: true, navigation: { pathname: "/login", query: {} } } },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("link", { name: "Continue with GitHub" })).toHaveAttribute(
      "href",
      "/api/v1/auth/github/login",
    );
  },
};

export const LoginLight: Story = { ...Login, globals: { theme: "light" } };

export const LoginWithError: Story = {
  render: () => <LoginScreen />,
  parameters: {
    nextjs: {
      appDirectory: true,
      navigation: { pathname: "/login", query: { error: "state_mismatch", next: "/acme" } },
    },
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("alert")).toHaveTextContent("started in another browser");
    await expect(canvas.getByRole("link", { name: "Continue with GitHub" })).toHaveAttribute(
      "href",
      "/api/v1/auth/github/login?next=%2Facme",
    );
  },
};

export const Profile: Story = {
  render: () => <ProfileView />,
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("heading", { name: "Octo Cat" })).toBeVisible();
  },
};

export const Sessions: Story = {
  render: () => <SessionsView />,
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("button", { name: "Revoke Safari on iOS" })).toBeVisible();
    await expect(canvas.getByRole("button", { name: "Sign out everywhere" })).toBeVisible();
  },
};

export const SessionsLight: Story = { ...Sessions, globals: { theme: "light" } };
