"use client";

import { useQueryClient } from "@tanstack/react-query";
import { MailOpen } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { Button, buttonVariants } from "@/components/ui/button";
import { $api, api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { queryKeys } from "@/lib/query/keys";
import { orgPath } from "@/lib/routes";

import { ROLE_DESCRIPTIONS, ROLE_LABELS } from "./roles";

const CLOSED: Record<string, { title: string; body: string }> = {
  expired: {
    title: "This invitation has expired",
    body: "Invitations last 7 days. Ask whoever invited you for a new link.",
  },
  accepted: {
    title: "This invitation was already used",
    body: "If it was you, you're already a member: open your organizations.",
  },
  revoked: {
    title: "This invitation was withdrawn",
    body: "Ask an admin of the organization if you still need access.",
  },
};

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex w-full max-w-md flex-col gap-5 rounded-xl border border-line bg-surface-1 p-8 shadow-dialog">
      {children}
    </div>
  );
}

export function InvitationView() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const preview = $api.useQuery("get", "/api/v1/invitations/{token}", {
    params: { path: { token } },
  });
  // Signed out is normal here (401): the page offers sign-in instead.
  const me = $api.useQuery("get", "/api/v1/me", {}, { retry: false });
  const [accepting, setAccepting] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  if (preview.isPending) return <DelayedSpinner active label="Loading invitation" />;
  if (preview.isError) {
    const notFound = preview.error instanceof ApiError && preview.error.status === 404;
    return notFound ? (
      <Card>
        <h1 className="text-lg font-semibold">This invitation link isn&rsquo;t valid</h1>
        <p className="text-sm text-fg-muted">
          Check that you copied the whole link, or ask for a new one.
        </p>
      </Card>
    ) : (
      <ErrorState title="Couldn't load the invitation" onRetry={() => void preview.refetch()} />
    );
  }

  const invitation = preview.data;
  const closed = CLOSED[invitation.status];
  const next = `/invite/${encodeURIComponent(token)}`;

  const accept = async () => {
    setAccepting(true);
    setError(null);
    try {
      const { data } = await api.POST("/api/v1/invitations/{token}/accept", {
        params: { path: { token } },
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.me() });
      router.push(orgPath(data?.slug ?? invitation.org_slug));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught : null);
      setAccepting(false);
    }
  };

  return (
    <Card>
      <MailOpen aria-hidden className="size-8 text-fg-faint" />
      <header className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold">
          {closed ? closed.title : `Join ${invitation.org_name}`}
        </h1>
        <p className="text-sm text-fg-muted">
          {closed
            ? closed.body
            : `${invitation.inviter_login ? `@${invitation.inviter_login}` : "Someone"} invited you as ${ROLE_LABELS[invitation.role].toLowerCase()}. ${ROLE_DESCRIPTIONS[invitation.role]}`}
        </p>
      </header>

      {!closed && error?.code === "invitation_wrong_account" && (
        <p role="alert" className="rounded-lg border border-warn/30 bg-warn/8 p-3 text-sm">
          This invitation was sent to a different account than the one you&rsquo;re signed in with
          {me.data ? ` (@${me.data.login})` : ""}. Sign out and sign in with the invited account.
        </p>
      )}
      {!closed && error && error.code !== "invitation_wrong_account" && (
        <p role="alert" className="text-sm text-danger">
          {error.message}
        </p>
      )}

      {closed ? (
        <Link href="/auth/callback" className={buttonVariants({ variant: "outline" })}>
          Go to your organizations
        </Link>
      ) : me.isPending ? null : me.data ? (
        error?.code === "invitation_wrong_account" ? (
          <Link href="/logout" className={buttonVariants({ variant: "outline" })}>
            Sign out
          </Link>
        ) : (
          <Button size="lg" disabled={accepting} onClick={() => void accept()}>
            {accepting ? "Joining…" : `Join ${invitation.org_name}`}
          </Button>
        )
      ) : (
        <Link
          href={`/login?next=${encodeURIComponent(next)}`}
          className={buttonVariants({ size: "lg" })}
        >
          Sign in to accept
        </Link>
      )}
    </Card>
  );
}
