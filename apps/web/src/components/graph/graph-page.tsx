"use client";

/**
 * `/[org]/[repo]/graph` (6.7): the dependency graph, four ways. **Graph**: the level's nodes and
 * edges on a WebGL canvas, explored by hover, focus, search and paths, zooming in opening a
 * node into the level below. **DSM**: the level as a matrix, cells in a cycle red. **Cycles**:
 * each cycle and the edges that close it. **Packages**: the third-party packages, their versions
 * and who imports them.
 */
import { Network } from "lucide-react";

import { EmptyState } from "@/components/feedback/empty-state";
import { LockedState } from "@/components/feedback/locked-state";
import { useRepoFeatures, useRepoSlug } from "@/components/repo/use-repo";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { STAGE_LABELS } from "@/lib/indexing";
import { repoPath } from "@/lib/routes";

import { CyclesView } from "./cycles-view";
import { DsmView } from "./dsm-view";
import { GraphExplorer } from "./graph-explorer";
import { GRAPH_TABS, useGraphState, type GraphTab } from "./graph-state";
import { PackagesView } from "./packages-view";

const TAB_LABELS: Record<GraphTab, string> = {
  graph: "Graph",
  dsm: "DSM",
  cycles: "Cycles",
  packages: "Packages",
};

export function GraphPage() {
  const { org, repo } = useRepoSlug();
  const { state, ready, active } = useRepoFeatures();
  if (!ready) return null;
  const graph = state("graph_view");
  if (!graph.available) {
    const stage = graph.stage ? STAGE_LABELS[graph.stage] : "indexing";
    return (
      <div className="mx-auto flex w-full max-w-3xl flex-col p-8">
        {active && graph.coming ? (
          <LockedState
            feature="The dependency graph"
            stage={stage}
            {...(graph.etaSeconds !== null ? { etaSeconds: graph.etaSeconds } : {})}
            href={repoPath(org, repo)}
          />
        ) : (
          <EmptyState
            icon={<Network aria-hidden />}
            title="The graph opens after indexing"
            description="It is drawn from the code graph, once its files are linked."
          />
        )}
      </div>
    );
  }
  return <Explorer />;
}

function Explorer() {
  const [state, setState] = useGraphState();
  return (
    <Tabs
      value={state.tab}
      onValueChange={(value: GraphTab) => void setState({ tab: value })}
      className="flex min-h-0 flex-1 flex-col"
    >
      <div className="flex items-center gap-3 border-b border-line px-4 py-2">
        <h1 className="text-sm font-medium">Dependencies</h1>
        <TabsList aria-label="Views">
          {GRAPH_TABS.map((tab) => (
            <TabsTrigger key={tab} value={tab}>
              {TAB_LABELS[tab]}
            </TabsTrigger>
          ))}
        </TabsList>
      </div>
      <TabsContent value="graph" className="flex min-h-0 flex-1 flex-col">
        <GraphExplorer />
      </TabsContent>
      <TabsContent value="dsm" className="min-h-0 flex-1 overflow-auto">
        <DsmView />
      </TabsContent>
      <TabsContent value="cycles" className="min-h-0 flex-1 overflow-auto">
        <CyclesView />
      </TabsContent>
      <TabsContent value="packages" className="min-h-0 flex-1 overflow-auto">
        <PackagesView />
      </TabsContent>
    </Tabs>
  );
}
