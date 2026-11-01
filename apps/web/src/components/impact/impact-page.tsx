"use client";

/**
 * `/[org]/[repo]/impact` (6.8): what a change may break. `?symbol=` (a key or a name) or
 * `?files=` (repeated) is the change; without one, a picker. The answer is drawn as a heat map,
 * the change at the center and each ring a hop further, the surest and nearest in red; beside it
 * the routes, screens, tests and tables affected, ranked, each with how sure and how far, and the
 * whole as a PR note.
 */
import { Radar } from "lucide-react";
import { useSearchParams } from "next/navigation";

import { EmptyState } from "@/components/feedback/empty-state";
import { LockedState } from "@/components/feedback/locked-state";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { EntryPicker, type PickerText } from "@/components/trace/entry-picker";
import { impactHref } from "@/lib/api/impact";
import { STAGE_LABELS } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";

import { ImpactView } from "./impact-view";

const PICKER: PickerText = {
  title: "What would a change break?",
  icon: <Radar aria-hidden className="size-5 text-fg-muted" />,
  description: (
    <>
      Pick what you would change (a function, a class, a component or a hook): everything that
      depends on it, ranked by how sure and how near, with the routes, screens, tests and tables it
      reaches.
    </>
  ),
  label: "What you would change",
  placeholder: "A function, a class, a component or a hook",
  listLabel: "Symbols to change",
  empty: "Nothing by that name. Press Enter to look it up as typed.",
};

export function ImpactPage() {
  const { org, repo } = useRepoSlug();
  const { state, ready, active } = useRepoFeatures();
  const params = useSearchParams();
  if (!ready) return null;
  const impact = state("callers");
  if (!impact.available) {
    const stage = impact.stage ? STAGE_LABELS[impact.stage] : "indexing";
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col p-8">
        {active && impact.coming ? (
          <LockedState
            feature="Impact"
            stage={stage}
            {...(impact.etaSeconds !== null ? { etaSeconds: impact.etaSeconds } : {})}
            href={repoPath(org, repo)}
          />
        ) : (
          <EmptyState
            icon={<Radar aria-hidden />}
            title="Impact opens after indexing"
            description="It is worked out from the code graph, once its files are linked."
          />
        )}
      </div>
    );
  }
  const symbol = params.get("symbol");
  const files = params.getAll("files");
  if (!symbol && files.length === 0) {
    return (
      <EntryPicker text={PICKER} href={(o, r, entry) => impactHref(o, r, { symbol: entry })} />
    );
  }
  return <ImpactView symbol={symbol} files={files} />;
}
