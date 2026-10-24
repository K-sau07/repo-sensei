"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  Keyboard,
  Link2,
  LogOut,
  PanelLeft,
  RotateCw,
  Route as TraceIcon,
  Settings,
  SunMoon,
  Wrench,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { toast } from "sonner";

import { refreshIndexState, startReindex } from "@/components/indexing/reindex";
import { writeClipboard } from "@/components/primitives/copy-button";
import { useShellStore } from "@/components/shell/shell-store";
import { signOut } from "@/components/shell/sign-out";
import { ApiError } from "@/lib/api/errors";
import { actionId } from "@/lib/palette/locations";
import { orgPath, repoPath } from "@/lib/routes";

import { isFullCommit, permalinkFor, type RepoContext, type ShownItem } from "./items";
import { renderedCommit } from "./rendered";

interface ActionScope {
  org: string;
  repo: RepoContext | null;
  /** The code browser is open (permalinks pin the commit there), at this path and `?ref=`. */
  code: boolean;
  path: string | null;
  ref: string | null;
}

/** The Do group: a small registry of actions, each shown only where it makes sense. */
export function useActions({ org, repo, code, path, ref }: ActionScope): ShownItem[] {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { resolvedTheme, setTheme } = useTheme();
  const toggleSidebar = useShellStore((state) => state.toggleSidebar);
  const setShortcutsOpen = useShellStore((state) => state.setShortcutsOpen);
  const light = resolvedTheme === "light";

  const items: ShownItem[] = [];
  if (repo?.admin) {
    const slug = repo.slug;
    items.push({
      id: actionId("reindex"),
      group: "do",
      kind: "action",
      title: `Reindex ${slug}`,
      keywords: ["index again", "update the index"],
      icon: RotateCw,
      scope: { org, repo: slug },
      // Until its runs have loaded, it isn't known whether one is under way.
      ...(!repo.featuresReady
        ? { disabled: { reason: "Checking…" } }
        : repo.indexing
          ? { disabled: { reason: "It's being indexed right now" } }
          : {}),
      run: () => {
        startReindex(org, slug)
          .then(() => {
            toast.success("Indexing started", {
              action: {
                label: "Watch",
                onClick: () => {
                  router.push(repoPath(org, slug, "index"));
                },
              },
            });
            return refreshIndexState(queryClient, org, slug);
          })
          .catch((error: unknown) => {
            toast.error(error instanceof ApiError ? error.message : "Couldn't start indexing.");
          });
      },
    });
  }
  if (repo) {
    const slug = repo.slug;
    const commits = () => ({
      rendered:
        code && path !== null
          ? renderedCommit(queryClient, { org, repo: slug, path, ref, version: repo.version })
          : null,
      indexed: repo.indexedSha,
    });
    const known = commits();
    const pinnable =
      !code ||
      known.rendered !== null ||
      (ref === null ? known.indexed !== null : isFullCommit(ref));
    items.push({
      id: actionId("copy-permalink"),
      group: "do",
      kind: "action",
      title: "Copy permalink",
      keywords: ["link", "url", "share"],
      icon: Link2,
      scope: { org, repo: repo.slug },
      ...(pinnable ? {} : { disabled: { reason: "Nothing is shown yet to pin it to" } }),
      run: () => {
        // Read again: the page may have shown more since the palette opened.
        writeClipboard(permalinkFor(window.location.href, code, commits()))
          .then(() => {
            toast.success("Permalink copied");
          })
          .catch(() => {
            toast.error("Couldn't copy the link.");
          });
      },
    });
  }
  items.push(
    {
      id: actionId("open-settings"),
      group: "do",
      kind: "action",
      title: "Open settings",
      keywords: ["organization", "configure"],
      icon: Settings,
      shortcut: "go-settings",
      run: () => {
        router.push(orgPath(org, "settings"));
      },
    },
    {
      id: actionId("toggle-theme"),
      group: "do",
      kind: "action",
      title: light ? "Switch to dark theme" : "Switch to light theme",
      keywords: ["theme", "appearance", "mode"],
      icon: SunMoon,
      run: () => {
        setTheme(light ? "dark" : "light");
      },
    },
    {
      id: actionId("toggle-sidebar"),
      group: "do",
      kind: "action",
      title: "Toggle the sidebar",
      keywords: ["collapse", "expand", "hide", "show"],
      icon: PanelLeft,
      shortcut: "toggle-sidebar",
      run: toggleSidebar,
    },
    {
      id: actionId("shortcuts"),
      group: "do",
      kind: "action",
      title: "Show keyboard shortcuts",
      keywords: ["keys", "hotkeys", "help"],
      icon: Keyboard,
      shortcut: "shortcuts",
      run: () => {
        setShortcutsOpen(true);
      },
    },
  );
  if (repo) {
    items.push(
      {
        id: actionId("trace"),
        group: "do",
        kind: "action",
        title: "Trace this",
        keywords: ["flow", "path", "follow"],
        icon: TraceIcon,
        disabled: { reason: "Tracing is coming soon" },
      },
      {
        id: actionId("new-fix"),
        group: "do",
        kind: "action",
        title: "New fix",
        keywords: ["bug", "repair", "patch"],
        icon: Wrench,
        disabled: { reason: "Fixes are coming soon" },
      },
    );
  }
  items.push({
    id: actionId("sign-out"),
    group: "do",
    kind: "action",
    title: "Sign out",
    keywords: ["leave", "exit"],
    icon: LogOut,
    destructive: true,
    run: () => void signOut(),
  });
  return items;
}
