"use client";

import { cn } from "cn";
import { X } from "lucide-react";
import { useEffect, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

import { useShellStore } from "./shell-store";

/** The docked, page-specific rail (evidence on Ask, agent activity on runs, symbol on code). */
export function RightRail({ className }: { className?: string }) {
  const title = useShellStore((state) => state.railTitle);
  const content = useShellStore((state) => state.railContent);
  const toggleRail = useShellStore((state) => state.toggleRail);
  return (
    <aside
      aria-label={title ?? "Details"}
      className={cn(
        "hidden w-80 shrink-0 flex-col border-l border-line bg-surface-1 in-data-[rail=open]:lg:flex",
        className,
      )}
    >
      <header className="flex h-10 items-center justify-between border-b border-line px-3">
        <h2 className="text-sm font-medium">{title ?? "Details"}</h2>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Close the right rail"
          onClick={toggleRail}
        >
          <X aria-hidden />
        </Button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-3 text-sm">
        {content ?? <p className="text-fg-muted">Nothing to show for this page.</p>}
      </div>
    </aside>
  );
}

/** Pages put their rail content here; it is cleared when the page unmounts. */
export function useRightRail(title: string, content: ReactNode): void {
  const setRail = useShellStore((state) => state.setRail);
  useEffect(() => {
    setRail(title, content);
    return () => {
      setRail(null, null);
    };
  }, [setRail, title, content]);
}
