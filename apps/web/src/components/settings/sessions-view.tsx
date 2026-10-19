"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Laptop, Smartphone } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { ErrorState } from "@/components/feedback/error-state";
import { RelativeTime } from "@/components/primitives/relative-time";
import { signOut } from "@/components/shell/sign-out";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { $api, api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import type { SessionInfo } from "@/lib/api/types";
import { queryKeys } from "@/lib/query/keys";

/** "Chrome on macOS" from a user agent (good enough to recognise your own devices). */
export function describeDevice(userAgent: string | null): { label: string; mobile: boolean } {
  if (!userAgent) return { label: "Unknown device", mobile: false };
  const browser = userAgent.includes("Edg/")
    ? "Edge"
    : userAgent.includes("Firefox/")
      ? "Firefox"
      : userAgent.includes("Chrome/")
        ? "Chrome"
        : userAgent.includes("Safari/")
          ? "Safari"
          : "Browser";
  const os = /iPhone|iPad/.test(userAgent)
    ? "iOS"
    : userAgent.includes("Android")
      ? "Android"
      : /Mac OS X|Macintosh/.test(userAgent)
        ? "macOS"
        : userAgent.includes("Windows")
          ? "Windows"
          : userAgent.includes("Linux")
            ? "Linux"
            : null;
  return {
    label: os ? `${browser} on ${os}` : browser,
    mobile: /iPhone|Android|Mobile/.test(userAgent),
  };
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong. Please try again.";
}

function SessionRow({ session }: { session: SessionInfo }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const { label, mobile } = describeDevice(session.user_agent);
  const Icon = mobile ? Smartphone : Laptop;

  const revoke = async () => {
    if (session.current) {
      await signOut();
      return;
    }
    setBusy(true);
    try {
      await api.DELETE("/api/v1/me/sessions/{session_id}", {
        params: { path: { session_id: session.id } },
      });
      toast.success(`Signed out of ${label}`);
      await queryClient.invalidateQueries({ queryKey: queryKeys.mySessions() });
    } catch (error) {
      toast.error(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="flex items-center gap-4 py-3">
      <Icon aria-hidden className="size-5 shrink-0 text-fg-muted" />
      <div className="min-w-0 flex-1 text-sm">
        <p className="flex items-center gap-2 font-medium">
          {label}
          {session.current && <Badge variant="secondary">This device</Badge>}
        </p>
        <p className="text-fg-muted">
          {session.ip ?? "Unknown address"} · last active{" "}
          <RelativeTime date={session.last_seen_at} />
        </p>
      </div>
      <Button variant="outline" size="sm" disabled={busy} onClick={() => void revoke()}>
        {session.current ? "Sign out" : "Revoke"}
        <span className="sr-only"> {label}</span>
      </Button>
    </li>
  );
}

function SignOutEverywhere() {
  const [busy, setBusy] = useState(false);
  const signOutAll = async () => {
    setBusy(true);
    try {
      await api.POST("/api/v1/auth/logout-all");
    } catch (error) {
      toast.error(errorMessage(error));
      setBusy(false);
      return;
    }
    // A full load on purpose: nothing from the signed-in session should stay in memory.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign("/login");
  };
  return (
    <Dialog>
      <DialogTrigger render={<Button variant="destructive" />}>Sign out everywhere</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Sign out everywhere?</DialogTitle>
          <DialogDescription>
            Every browser and device signed in to your account is signed out, including this one.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
          <Button variant="destructive" disabled={busy} onClick={() => void signOutAll()}>
            Sign out everywhere
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function SessionsView() {
  const sessions = $api.useQuery("get", "/api/v1/me/sessions");
  if (sessions.isPending) {
    return (
      <div role="status" aria-label="Loading sessions" className="flex flex-col gap-2">
        <Skeleton className="h-row" />
        <Skeleton className="h-row" />
      </div>
    );
  }
  if (sessions.isError) {
    return (
      <ErrorState title="Couldn't load your sessions" onRetry={() => void sessions.refetch()} />
    );
  }
  return (
    <section aria-labelledby="sessions-heading" className="flex flex-col gap-4">
      <div>
        <h2 id="sessions-heading" className="text-lg font-semibold">
          Where you&rsquo;re signed in
        </h2>
        <p className="text-sm text-fg-muted">
          Revoke any session you don&rsquo;t recognise. Sessions end on their own after 14 days
          without use.
        </p>
      </div>
      <ul className="divide-y divide-line rounded-lg border border-line px-4">
        {sessions.data.map((session) => (
          <SessionRow key={session.id} session={session} />
        ))}
      </ul>
      <div>
        <SignOutEverywhere />
      </div>
    </section>
  );
}
