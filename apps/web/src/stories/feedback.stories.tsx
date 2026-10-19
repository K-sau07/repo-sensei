import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { FolderGit2 } from "lucide-react";
import type { Route } from "next";
import { expect, fn, userEvent } from "storybook/test";

import { OfflineBanner, PartialBanner, StaleBadge } from "@/components/feedback/banners";
import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { LockedState } from "@/components/feedback/locked-state";
import { PermissionState } from "@/components/feedback/permission-state";
import { Button } from "@/components/ui/button";

const ORG_HOME = "/acme" as Route;

const meta = { title: "Feedback/States" } satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const Empty: Story = {
  render: () => (
    <EmptyState
      icon={<FolderGit2 />}
      title="No repositories yet"
      description="Connect a GitHub repository or paste a public URL to start indexing."
      action={<Button>Connect a repository</Button>}
    />
  ),
};

const retry = fn();

const renderError = () => (
  <ErrorState
    title="Couldn't load members"
    description="The server didn't answer in time."
    onRetry={retry}
    requestId="01a1523c-c456-7493-a380-b7b874a0d4da"
  />
);

export const Error: Story = {
  render: renderError,
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("alert")).toHaveTextContent("Couldn't load members");
    await userEvent.click(canvas.getByRole("button", { name: "Try again" }));
    await expect(retry).toHaveBeenCalledOnce();
  },
};

export const ErrorWithoutRetry: Story = {
  render: () => <ErrorState title="This answer couldn't be verified" />,
};

export const Permission: Story = {
  render: () => (
    <PermissionState
      requiredRole="admin"
      yourRole="member"
      action={<Button variant="outline">Request access</Button>}
    />
  ),
};

export const Locked: Story = {
  render: () => (
    <LockedState feature="Trace" stage="Cross-layer links" etaSeconds={190} href={ORG_HOME} />
  ),
};

export const LockedWithoutEta: Story = {
  render: () => <LockedState feature="Wiki" stage="Summaries" href={ORG_HOME} />,
};

export const Banners: Story = {
  render: () => (
    <div className="flex max-w-2xl flex-col gap-4">
      <PartialBanner
        action={
          <Button size="sm" variant="ghost">
            Retry
          </Button>
        }
      >
        Results from 2 of 14 repositories are missing.
      </PartialBanner>
      <div>
        <StaleBadge>3 commits behind main</StaleBadge>
      </div>
      <OfflineBanner forceOffline />
    </div>
  ),
};

export const BannersLight: Story = { ...Banners, globals: { theme: "light" } };
export const ErrorLight: Story = { render: renderError, globals: { theme: "light" } };
