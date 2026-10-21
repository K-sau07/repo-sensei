import {
  Activity,
  BookOpen,
  Code2,
  FileStack,
  GitBranch,
  GitPullRequestArrow,
  LayoutDashboard,
  Map as MapIcon,
  MessageSquareText,
  Network,
  Orbit,
  Route as RouteIcon,
  Settings,
  Workflow,
  Wrench,
  type LucideIcon,
} from "lucide-react";

import type { Feature } from "@/lib/indexing";
import type { ShortcutId } from "@/lib/shortcut-registry";

export interface RepoTab {
  id: string;
  label: string;
  /** The path under `/[org]/[repo]` ("" for the overview). */
  section: string;
  icon: LucideIcon;
  /** The feature it needs; locked until its indexing stage is done. */
  feature?: Feature;
  /** The phase that builds the page, for "coming in phase X". */
  phase?: number;
  shortcut?: ShortcutId;
  adminOnly?: boolean;
}

const OVERVIEW: RepoTab = {
  id: "overview",
  label: "Overview",
  section: "",
  icon: LayoutDashboard,
  shortcut: "repo-overview",
};

export const PRIMARY_TABS: RepoTab[] = [
  OVERVIEW,
  {
    id: "ask",
    label: "Ask",
    section: "ask",
    icon: MessageSquareText,
    feature: "ask",
    phase: 4,
    shortcut: "repo-ask",
  },
  {
    id: "code",
    label: "Code",
    section: "code",
    icon: Code2,
    feature: "code_browser",
    shortcut: "repo-code",
  },
  {
    id: "trace",
    label: "Trace",
    section: "trace",
    icon: RouteIcon,
    feature: "trace",
    phase: 3,
    shortcut: "repo-trace",
  },
  {
    id: "graph",
    label: "Graph",
    section: "graph",
    icon: Network,
    feature: "graph_view",
    phase: 6,
    shortcut: "repo-graph",
  },
  {
    id: "diagrams",
    label: "Diagrams",
    section: "diagrams",
    icon: Workflow,
    feature: "diagrams",
    phase: 6,
    shortcut: "repo-diagrams",
  },
  {
    id: "fixes",
    label: "Fixes",
    section: "fixes",
    icon: Wrench,
    feature: "fixes",
    phase: 8,
    shortcut: "repo-fixes",
  },
  {
    id: "changes",
    label: "Changes",
    section: "changes",
    icon: GitPullRequestArrow,
    feature: "changes",
    phase: 9,
  },
  { id: "runs", label: "Runs", section: "runs", icon: Activity, phase: 8 },
];

export const OVERFLOW_TABS: RepoTab[] = [
  { id: "map", label: "Map", section: "map", icon: Orbit, feature: "semantic_search", phase: 6 },
  { id: "wiki", label: "Wiki", section: "wiki", icon: BookOpen, feature: "wiki", phase: 4 },
  { id: "impact", label: "Impact", section: "impact", icon: MapIcon, feature: "callers", phase: 6 },
  { id: "index", label: "Index", section: "index", icon: FileStack },
  { id: "settings", label: "Settings", section: "settings", icon: Settings, adminOnly: true },
];

export const ALL_TABS = [...PRIMARY_TABS, ...OVERFLOW_TABS];

/** The tab a path under the repository belongs to. */
export function tabForPath(rest: string): RepoTab {
  const first = rest.split("/")[0] ?? "";
  return ALL_TABS.find((tab) => tab.section === first) ?? OVERVIEW;
}

export const BRANCH_ICON = GitBranch;

/** The tab a feature lives on (for "available now →" links). */
export function tabForFeature(feature: Feature): RepoTab | undefined {
  return ALL_TABS.find((tab) => tab.feature === feature);
}
