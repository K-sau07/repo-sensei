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
  const { setCommandOpen, setShortcutsOpen, toggleSidebar, toggleRail } = useShellStore();
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
      home: go(""),
      inbox: go("inbox"),
      activity: go("activity"),
      settings: go("settings"),
    };
  }, [router, org, setCommandOpen, setShortcutsOpen]);

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
  useShortcut("toggle-sidebar", toggleSidebar);
  useShortcut("toggle-rail", toggleRail);
  useShortcut("go-home", handlers.home);
  useShortcut("go-inbox", handlers.inbox);
  useShortcut("go-activity", handlers.activity);
  useShortcut("go-settings", handlers.settings);
  return null;
}
