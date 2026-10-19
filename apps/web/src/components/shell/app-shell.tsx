"use client";

import { Suspense, useEffect, type ReactNode } from "react";

import { CommandBar } from "./command-bar";
import { MobileBottomBar, MobileTopBar } from "./mobile-nav";
import { RightRail } from "./right-rail";
import { ShellHotkeys } from "./shell-hotkeys";
import { BottomBarSkeleton, SidebarSkeleton, TopBarSkeleton } from "./shell-skeleton";
import { useShellStore } from "./shell-store";
import { ShortcutsSheet } from "./shortcuts-sheet";
import { Sidebar } from "./sidebar";

/**
 * The frame every org page lives in: sidebar, main column, right rail and global overlays.
 *
 * Only the parts that read the org from the URL suspend, each behind a same-sized fallback;
 * `children` stays outside every boundary so pages keep their static shell and instant navigation.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const hydrate = useShellStore((state) => state.hydrate);
  useEffect(() => {
    hydrate();
  }, [hydrate]);

  return (
    <div className="flex h-dvh min-h-0 w-full">
      <a
        href="#main"
        className="sr-only z-50 rounded-sm bg-surface-2 px-3 py-2 text-sm focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Skip to content
      </a>
      <Suspense fallback={<SidebarSkeleton />}>
        <Sidebar className="hidden lg:flex" />
      </Suspense>
      <div className="flex min-w-0 flex-1 flex-col">
        <Suspense fallback={<TopBarSkeleton />}>
          <MobileTopBar className="lg:hidden" />
        </Suspense>
        <main id="main" tabIndex={-1} className="min-h-0 flex-1 overflow-y-auto outline-none">
          {children}
        </main>
        <Suspense fallback={<BottomBarSkeleton />}>
          <MobileBottomBar className="lg:hidden" />
        </Suspense>
      </div>
      <RightRail />
      <ShortcutsSheet />
      <Suspense fallback={null}>
        <CommandBar />
        <ShellHotkeys />
      </Suspense>
    </div>
  );
}
