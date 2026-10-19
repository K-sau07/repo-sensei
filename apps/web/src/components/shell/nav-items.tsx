import {
  Activity,
  ChartLine,
  FolderGit2,
  House,
  Inbox,
  Settings,
  type LucideIcon,
} from "lucide-react";

import type { OrgSection } from "@/lib/routes";
import type { ShortcutId } from "@/lib/shortcut-registry";

export interface NavItem {
  label: string;
  section: OrgSection;
  icon: LucideIcon;
  shortcut?: ShortcutId;
}

export const PRIMARY_NAV: NavItem[] = [
  { label: "Home", section: "", icon: House, shortcut: "go-home" },
  { label: "Inbox", section: "inbox", icon: Inbox, shortcut: "go-inbox" },
  { label: "Activity", section: "activity", icon: Activity, shortcut: "go-activity" },
];

export const SECONDARY_NAV: NavItem[] = [
  { label: "Repositories", section: "repos", icon: FolderGit2 },
  { label: "Insights", section: "insights", icon: ChartLine },
  { label: "Settings", section: "settings", icon: Settings, shortcut: "go-settings" },
];
