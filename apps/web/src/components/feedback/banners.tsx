"use client";

import { cn } from "cn";
import { CloudOff, History, TriangleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { useOnlineStatus } from "@/hooks/use-online-status";

interface PartialBannerProps {
  /** What is missing: "Results from 2 of 14 repositories are missing." */
  children: ReactNode;
  action?: ReactNode;
  className?: string;
}

/** Some of the data loaded, some didn't; the page still works with what it has. */
export function PartialBanner({ children, action, className }: PartialBannerProps) {
  return (
    <div
      role="status"
      className={cn(
        "flex items-center gap-3 rounded-lg border border-warn/30 bg-warn/8 px-3 py-2 text-sm",
        className,
      )}
    >
      <TriangleAlert aria-hidden className="size-4 shrink-0 text-warn" />
      <div className="flex-1">{children}</div>
      {action}
    </div>
  );
}

interface StaleBadgeProps {
  /** "3 commits behind main", "Indexed 2 days ago". */
  children: ReactNode;
  className?: string;
}

/** The data shown is older than the code it describes. */
export function StaleBadge({ children, className }: StaleBadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center gap-1 rounded-sm border border-warn/30 px-1.5 text-xs text-warn",
        className,
      )}
    >
      <History aria-hidden className="size-3.5" />
      {children}
    </span>
  );
}

/** Appears while the browser is offline; live updates resume on reconnect. */
export function OfflineBanner({
  forceOffline = false,
  className,
}: {
  /** Show regardless of the network (stories, tests). */
  forceOffline?: boolean;
  className?: string;
}) {
  const online = useOnlineStatus();
  if (online && !forceOffline) return null;
  return (
    <div
      role="status"
      className={cn(
        "flex items-center justify-center gap-2 bg-surface-3 px-3 py-1.5 text-sm text-fg",
        className,
      )}
    >
      <CloudOff aria-hidden className="size-4" />
      You&apos;re offline. Live updates resume when you reconnect.
    </div>
  );
}
