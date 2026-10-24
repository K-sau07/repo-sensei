/**
 * Every keyboard shortcut in the app, in one place: the hotkey hooks, the `?` sheet and the ⌘K
 * palette all read from here, so what we show is always what works.
 *
 * Keys use the notation of `lib/shortcuts.ts` ("mod+k", "g h", "?").
 */

export type ShortcutGroup = "General" | "Navigation" | "View";

export interface Shortcut {
  id: ShortcutId;
  keys: string;
  label: string;
  group: ShortcutGroup;
}

export const SHORTCUTS = [
  { id: "command", keys: "mod+k", label: "Open the command palette", group: "General" },
  { id: "shortcuts", keys: "?", label: "Show keyboard shortcuts", group: "General" },
  { id: "switch-org", keys: "mod+shift+o", label: "Switch organization", group: "General" },
  { id: "go-home", keys: "g h", label: "Go to home", group: "Navigation" },
  { id: "go-inbox", keys: "g i", label: "Go to inbox", group: "Navigation" },
  { id: "go-activity", keys: "g y", label: "Go to activity", group: "Navigation" },
  { id: "go-settings", keys: "g s", label: "Go to settings", group: "Navigation" },
  { id: "repo-overview", keys: "g o", label: "Repository: overview", group: "Navigation" },
  { id: "repo-ask", keys: "g a", label: "Repository: ask", group: "Navigation" },
  { id: "repo-code", keys: "g c", label: "Repository: code", group: "Navigation" },
  { id: "repo-trace", keys: "g t", label: "Repository: trace", group: "Navigation" },
  { id: "repo-graph", keys: "g g", label: "Repository: graph", group: "Navigation" },
  { id: "repo-diagrams", keys: "g d", label: "Repository: diagrams", group: "Navigation" },
  { id: "repo-fixes", keys: "g f", label: "Repository: fixes", group: "Navigation" },
  { id: "list-next", keys: "j", label: "Next item in a list", group: "Navigation" },
  { id: "list-prev", keys: "k", label: "Previous item in a list", group: "Navigation" },
  { id: "list-open", keys: "enter", label: "Open the focused item", group: "Navigation" },
  { id: "lane-prev", keys: "left", label: "Previous lane of search results", group: "Navigation" },
  { id: "lane-next", keys: "right", label: "Next lane of search results", group: "Navigation" },
  { id: "focus-search", keys: "/", label: "Focus the search field", group: "Navigation" },
  { id: "trace", keys: "t", label: "Trace the focused result (coming soon)", group: "Navigation" },
  { id: "toggle-sidebar", keys: "[", label: "Toggle the sidebar", group: "View" },
  { id: "toggle-rail", keys: "]", label: "Toggle the right rail", group: "View" },
  { id: "code-search", keys: "mod+f", label: "Search in the open file", group: "View" },
] as const satisfies readonly { id: string; keys: string; label: string; group: ShortcutGroup }[];

export type ShortcutId = (typeof SHORTCUTS)[number]["id"];

export function shortcut(id: ShortcutId): Shortcut {
  const found = SHORTCUTS.find((item) => item.id === id);
  if (!found) throw new Error(`unknown shortcut: ${id}`);
  return found;
}

/** Our notation → react-hotkeys-hook's: sequences join with ">" ("g h" → "g>h"). */
export function toHotkey(keys: string): string {
  return keys.trim().split(/\s+/).join(">");
}

/** Single characters typed with Shift ("?") must match the produced key, not the physical one. */
export function matchesProducedKey(keys: string): boolean {
  return keys.length === 1 && !/[a-z0-9]/i.test(keys);
}
