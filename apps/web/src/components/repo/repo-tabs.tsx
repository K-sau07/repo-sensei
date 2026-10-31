"use client";

import { cn } from "cn";
import { Lock, MoreHorizontal } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { formatEta } from "@/components/feedback/locked-state";
import { KeyHint } from "@/components/primitives/key-hint";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { useShortcut } from "@/hooks/use-shortcut";
import { STAGE_LABELS } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";
import { shortcut, type ShortcutId } from "@/lib/shortcut-registry";

import { OVERFLOW_TABS, PRIMARY_TABS, tabForPath, type RepoTab } from "./repo-tabs-config";
import { useViewRef, withRef } from "./use-ref";
import { useRepoFeatures, useRepoSlug, type FeatureState } from "./use-repo";

/** Why a tab is locked, for its tooltip: "Available when Embeddings finishes · ~6m" while a run
 * that brings it is under way; otherwise when it arrives. */
export function lockedReason(state: FeatureState, phase?: number): string {
  const stage = state.stage ? STAGE_LABELS[state.stage] : "indexing";
  if (state.coming) {
    const eta = state.etaSeconds === null ? "" : ` · ~${formatEta(state.etaSeconds)}`;
    return `Available when ${stage} finishes${eta}`;
  }
  return phase ? `Arrives in phase ${String(phase)}` : `Available once ${stage} has run`;
}

function TabShortcut({ id, href }: { id: ShortcutId; href: Route }) {
  const router = useRouter();
  useShortcut(id, () => {
    router.push(href);
  });
  return null;
}

function TabLink({ tab, current }: { tab: RepoTab; current: boolean }) {
  const { org, repo } = useRepoSlug();
  const { state } = useRepoFeatures();
  const [ref] = useViewRef();
  const href = withRef(repoPath(org, repo, tab.section), ref);
  const feature = tab.feature ? state(tab.feature) : null;
  const locked = feature !== null && !feature.available;
  const Icon = tab.icon;
  const link = (
    <Link
      href={href}
      aria-current={current ? "page" : undefined}
      className={cn(
        "relative inline-flex h-control-md items-center gap-1.5 rounded-sm px-2.5 text-sm whitespace-nowrap focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
        current ? "text-fg" : "text-fg-muted hover:bg-surface-2 hover:text-fg",
        locked && "text-fg-faint",
        current &&
          "after:absolute after:inset-x-2 after:-bottom-[7px] after:h-0.5 after:rounded-full after:bg-fg",
      )}
    >
      <Icon aria-hidden className="size-4" />
      {tab.label}
      {locked && (
        <>
          <Lock aria-hidden className="size-3" />
          <span className="sr-only">(locked: {lockedReason(feature, tab.phase)})</span>
        </>
      )}
    </Link>
  );
  return (
    <li>
      {tab.shortcut && <TabShortcut id={tab.shortcut} href={href} />}
      {locked || tab.shortcut ? (
        <Tooltip>
          <TooltipTrigger render={link} />
          <TooltipContent>
            {locked ? lockedReason(feature, tab.phase) : tab.label}
            {tab.shortcut && <KeyHint shortcut={shortcut(tab.shortcut).keys} />}
          </TooltipContent>
        </Tooltip>
      ) : (
        link
      )}
    </li>
  );
}

/** Overview · Ask · Code · … tabs, plus Map, Wiki, Impact, Index and Settings in "More".
 * Tabs whose feature isn't indexed yet show a lock and say when they unlock. */
export function RepoTabs() {
  const { org, repo } = useRepoSlug();
  const pathname = usePathname();
  const router = useRouter();
  const { role } = useOrgRole();
  const { state } = useRepoFeatures();
  const [ref] = useViewRef();
  const base = repoPath(org, repo);
  const rest = pathname.startsWith(`${base}/`) ? pathname.slice(base.length + 1) : "";
  const current = tabForPath(rest);
  const overflow = OVERFLOW_TABS.filter((tab) => !tab.adminOnly || roleAtLeast(role, "admin"));
  const overflowCurrent = overflow.find((tab) => tab.id === current.id);

  return (
    <nav aria-label="Repository" className="border-b border-line px-4">
      <ul className="-mb-px flex items-center gap-0.5 overflow-x-auto py-1.5">
        {PRIMARY_TABS.map((tab) => (
          <TabLink key={tab.id} tab={tab} current={tab.id === current.id} />
        ))}
        <li>
          <DropdownMenu>
            <DropdownMenuTrigger
              className={cn(
                "inline-flex h-control-md items-center gap-1.5 rounded-sm px-2.5 text-sm text-fg-muted hover:bg-surface-2 hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
                overflowCurrent && "text-fg",
              )}
            >
              {overflowCurrent ? overflowCurrent.label : "More"}
              <MoreHorizontal aria-hidden className="size-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              {overflow.map((tab) => {
                const feature = tab.feature ? state(tab.feature) : null;
                const locked = feature !== null && !feature.available;
                const Icon = tab.icon;
                return (
                  <DropdownMenuItem
                    key={tab.id}
                    onClick={() => {
                      router.push(withRef(repoPath(org, repo, tab.section), ref));
                    }}
                  >
                    <Icon aria-hidden className="size-4" />
                    <span className="flex-1">{tab.label}</span>
                    {locked && (
                      <>
                        <Lock aria-hidden className="size-3 text-fg-faint" />
                        <span className="sr-only">(locked)</span>
                      </>
                    )}
                  </DropdownMenuItem>
                );
              })}
            </DropdownMenuContent>
          </DropdownMenu>
        </li>
      </ul>
    </nav>
  );
}
