import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { Users } from "lucide-react";
import { delay, http, HttpResponse } from "msw";
import { expect } from "storybook/test";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { apiError, handlers } from "@/mocks/handlers";

/** A small real consumer of the typed client, to exercise every query state against MSW. */
function MembersList({ org }: { org: string }) {
  const query = $api.useQuery("get", "/api/v1/orgs/{org}/members", {
    params: { path: { org } },
  });

  if (query.isPending) {
    return (
      <div role="status" aria-label="Loading members" className="flex max-w-md flex-col gap-2">
        <Skeleton className="h-row" />
        <Skeleton className="h-row" />
      </div>
    );
  }
  if (query.isError) {
    const error = query.error as unknown;
    return (
      <ErrorState
        title="Couldn't load members"
        description={error instanceof ApiError ? error.message : undefined}
        onRetry={() => void query.refetch()}
        {...(error instanceof ApiError && error.requestId ? { requestId: error.requestId } : {})}
      />
    );
  }
  if (query.data.length === 0) {
    return <EmptyState icon={<Users />} title="No members yet" />;
  }
  return (
    <ul className="flex max-w-md flex-col divide-y divide-line rounded-lg border border-line">
      {query.data.map((member) => (
        <li key={member.user_id} className="flex h-row items-center justify-between px-3 text-sm">
          {member.login}
          <Badge variant="secondary">{member.role}</Badge>
        </li>
      ))}
    </ul>
  );
}

const meta = {
  title: "Data/Members query (mock API)",
  component: MembersList,
  args: { org: "acme" },
} satisfies Meta<typeof MembersList>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Success: Story = {
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("octo")).toBeInTheDocument();
    await expect(canvas.getByText("hubot")).toBeInTheDocument();
  },
};

export const Empty: Story = {
  parameters: {
    msw: [http.get("*/api/v1/orgs/:org/members", () => HttpResponse.json([])), ...handlers],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByText("No members yet")).toBeInTheDocument();
  },
};

export const Failure: Story = {
  parameters: {
    msw: [
      http.get("*/api/v1/orgs/:org/members", () =>
        apiError(403, "insufficient_role", "This needs the admin role."),
      ),
      ...handlers,
    ],
  },
  play: async ({ canvas }) => {
    await expect(await canvas.findByRole("alert")).toHaveTextContent("This needs the admin role.");
    await expect(canvas.getByText("mock-request-id")).toBeInTheDocument();
  },
};

export const Loading: Story = {
  parameters: {
    msw: [http.get("*/api/v1/orgs/:org/members", () => delay("infinite")), ...handlers],
  },
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("status", { name: "Loading members" })).toBeInTheDocument();
  },
};
