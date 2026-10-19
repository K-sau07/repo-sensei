"use client";

import { ErrorState } from "@/components/feedback/error-state";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Skeleton } from "@/components/ui/skeleton";
import { useMe } from "@/hooks/use-me";

function Field({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] items-baseline gap-4 py-3 text-sm">
      <dt className="text-fg-muted">{label}</dt>
      <dd>{value ?? <span className="text-fg-faint">Not set</span>}</dd>
    </div>
  );
}

export function ProfileView() {
  const me = useMe();
  if (me.isPending) {
    return (
      <div role="status" aria-label="Loading your profile" className="flex flex-col gap-3">
        <Skeleton className="size-16 rounded-full" />
        <Skeleton className="h-row" />
        <Skeleton className="h-row" />
      </div>
    );
  }
  if (me.isError) {
    return <ErrorState title="Couldn't load your profile" onRetry={() => void me.refetch()} />;
  }
  const user = me.data;
  return (
    <section aria-labelledby="profile-heading" className="flex flex-col gap-6">
      <div className="flex items-center gap-4">
        <Avatar className="size-16" aria-hidden>
          {user.avatar_url && <AvatarImage src={user.avatar_url} alt="" />}
          <AvatarFallback className="text-xl">
            {(user.name ?? user.login).charAt(0).toUpperCase()}
          </AvatarFallback>
        </Avatar>
        <div>
          <h2 id="profile-heading" className="text-lg font-semibold">
            {user.name ?? user.login}
          </h2>
          <p className="text-sm text-fg-muted">@{user.login}</p>
        </div>
      </div>
      <dl className="divide-y divide-line rounded-lg border border-line px-4">
        <Field label="Name" value={user.name} />
        <Field label="GitHub username" value={user.login} />
        <Field label="Email" value={user.email} />
        <Field label="Notifications go to" value={user.email} />
      </dl>
      <p className="text-sm text-fg-muted">
        These come from your GitHub profile. Change them on GitHub, then sign in again to refresh.
      </p>
    </section>
  );
}
