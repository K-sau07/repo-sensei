"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo } from "react";

import { useShortcut } from "@/hooks/use-shortcut";
import { orgPath, type OrgSection } from "@/lib/routes";

import { useShellStore } from "./shell-store";
import { useOrgSlug } from "./use-org-slug";

/** Global shortcuts for the app shell (bindings come from the shortcut registry). */
export function ShellHotkeys() {
  const org = useOrgSlug();
  const router = useRouter();
  // Actions only (stable), so this never re-renders on layout changes.
  const setCommandOpen = useShellStore((state) => state.setCommandOpen);
  const setShortcutsOpen = useShellStore((state) => state.setShortcutsOpen);
  const setOrgSwitcherOpen = useShellStore((state) => state.setOrgSwitcherOpen);
  const toggleSidebar = useShellStore((state) => state.toggleSidebar);
  const toggleRail = useShellStore((state) => state.toggleRail);
  const handlers = useMemo(() => {
    const go = (section: OrgSection) => () => {
      router.push(orgPath(org, section));
    };
    return {
      command: () => {
        setCommandOpen(!useShellStore.getState().commandOpen);
      },
      shortcuts: () => {
        setShortcutsOpen(true);
      },
      switchOrg: () => {
        setOrgSwitcherOpen(true);
      },
      home: go(""),
      inbox: go("inbox"),
      activity: go("activity"),
      settings: go("settings"),
    };
  }, [router, org, setCommandOpen, setShortcutsOpen, setOrgSwitcherOpen]);

  // Marks the moment shortcuts work (tests wait for it).
  useEffect(() => {
    document.documentElement.dataset.shellReady = "true";
    return () => {
      delete document.documentElement.dataset.shellReady;
    };
  }, []);

  // ⌘K also works while typing; plain-key shortcuts never do.
  useShortcut("command", handlers.command, { inInputs: true });
  useShortcut("shortcuts", handlers.shortcuts);
  useShortcut("switch-org", handlers.switchOrg, { inInputs: true });
  useShortcut("toggle-sidebar", toggleSidebar);
  useShortcut("toggle-rail", toggleRail);
  useShortcut("go-home", handlers.home);
  useShortcut("go-inbox", handlers.inbox);
  useShortcut("go-activity", handlers.activity);
  useShortcut("go-settings", handlers.settings);
  return null;
}
