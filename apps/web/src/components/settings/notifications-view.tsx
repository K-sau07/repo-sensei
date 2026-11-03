"use client";

/**
 * `/settings/notifications` (8.10): what the signed-in person is told, and how, per kind: in the
 * inbox, by email. The same in every org they're in. Each switch saves as it is flipped. An
 * org's Slack is its admins' to set (in the org's settings).
 */
import { useQueryClient } from "@tanstack/react-query";
import { useId } from "react";
import { toast } from "sonner";

import { ErrorState } from "@/components/feedback/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { KindPreference, NotificationKind, Preferences } from "@/lib/api/inbox";
import { queryKeys } from "@/lib/query/keys";

const PATH = "/api/v1/me/notifications";

export function NotificationsView() {
  const preferences = $api.useQuery("get", PATH);
  const queryClient = useQueryClient();
  const save = $api.useMutation("patch", PATH, {
    onSuccess: (data) => {
      queryClient.setQueryData(queryKeys.myNotifications(), data);
    },
  });
  if (preferences.isPending) {
    return (
      <div role="status" aria-label="Loading your notifications" className="flex flex-col gap-3">
        <Skeleton className="h-row" />
        <Skeleton className="h-row" />
        <Skeleton className="h-row" />
      </div>
    );
  }
  if (preferences.isError) {
    return (
      <ErrorState
        title="Couldn't load your notifications"
        onRetry={() => void preferences.refetch()}
      />
    );
  }
  const data = preferences.data;
  const change = (kind: NotificationKind, channel: "in_app" | "email", on: boolean) => {
    const before = queryClient.getQueryData<Preferences>(queryKeys.myNotifications());
    // Flipped at once; put back if it couldn't be saved.
    queryClient.setQueryData<Preferences>(queryKeys.myNotifications(), (current) =>
      current
        ? {
            ...current,
            kinds: current.kinds.map((k) => (k.kind === kind ? { ...k, [channel]: on } : k)),
          }
        : current,
    );
    save.mutate(
      { body: { kinds: { [kind]: { [channel]: on } } } },
      {
        onError: (error) => {
          queryClient.setQueryData(queryKeys.myNotifications(), before);
          toast.error(error instanceof ApiError ? error.message : "That couldn't be saved.");
        },
      },
    );
  };
  return (
    <section aria-labelledby="notifications-heading" className="flex flex-col gap-4">
      <div>
        <h2 id="notifications-heading" className="text-lg font-semibold">
          Notifications
        </h2>
        <p className="text-sm text-fg-muted">
          What you&apos;re told, in every organization you&apos;re in.{" "}
          {data.email ? (
            <>
              Email goes to <span className="text-fg">{data.email}</span> (from your GitHub
              profile).
            </>
          ) : (
            "Your GitHub profile has no email address, so nothing is emailed."
          )}
        </p>
      </div>
      <table className="w-full text-sm" data-testid="notification-preferences">
        <thead>
          <tr className="border-b border-line text-left text-xs text-fg-muted">
            <th scope="col" className="py-2 font-normal">
              Kind
            </th>
            <th scope="col" className="w-28 py-2 text-center font-normal">
              In the inbox
            </th>
            <th scope="col" className="w-28 py-2 text-center font-normal">
              By email
            </th>
          </tr>
        </thead>
        <tbody>
          {data.kinds.map((kind) => (
            <KindRow key={kind.kind} kind={kind} email={data.email !== null} onChange={change} />
          ))}
        </tbody>
      </table>
      <p className="text-xs text-fg-muted">
        An organization&apos;s Slack channel is set by its admins, in the organization&apos;s
        settings.
      </p>
    </section>
  );
}

function KindRow({
  kind,
  email,
  onChange,
}: {
  kind: KindPreference;
  email: boolean;
  onChange: (kind: NotificationKind, channel: "in_app" | "email", on: boolean) => void;
}) {
  const id = useId();
  return (
    <tr className="border-b border-line last:border-0">
      <th scope="row" id={id} className="py-2.5 text-left font-normal">
        {kind.label}
      </th>
      <td className="text-center">
        <Switch
          checked={kind.in_app}
          aria-label={`${kind.label}: in the inbox`}
          onCheckedChange={(on: boolean) => {
            onChange(kind.kind, "in_app", on);
          }}
        />
      </td>
      <td className="text-center">
        <Switch
          checked={email && kind.email}
          disabled={!email}
          aria-label={`${kind.label}: by email`}
          onCheckedChange={(on: boolean) => {
            onChange(kind.kind, "email", on);
          }}
        />
      </td>
    </tr>
  );
}
