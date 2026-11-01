/**
 * The explorer's layout of a level (6.7): the server's (a spring layout, the same each time,
 * cached with the graph) refined in the browser by ForceAtlas2 for a level of up to
 * {@link MAX_REFINED} nodes. A spring layout leaves a level's tightly coupled core in a clump and
 * its loose nodes at the rim; ForceAtlas2's LinLog mode spreads the core into its clusters and its
 * strong gravity brings the loose nodes in. It starts from the server's positions and has no
 * randomness, so a level is drawn the same each time. Positions stay within [-1, 1].
 */
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";

import type { Deps } from "@/lib/api/deps";

export const MAX_REFINED = 2_000;
const SETTINGS = { gravity: 0.5, scalingRatio: 20, strongGravityMode: true, linLogMode: true };

export function refineLayout(deps: Deps): Deps {
  if (deps.nodes.length < 3 || deps.nodes.length > MAX_REFINED) return deps;
  const graph = new Graph({ type: "directed", allowSelfLoops: false });
  for (const node of deps.nodes) graph.addNode(node.id, { x: node.x, y: node.y, size: 1 });
  for (const edge of deps.edges) {
    if (edge.source === edge.target || graph.hasEdge(edge.source, edge.target)) continue;
    if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
    graph.addEdge(edge.source, edge.target, { weight: 1 + Math.log1p(edge.weight) });
  }
  forceAtlas2.assign(graph, {
    iterations: graph.order > 500 ? 150 : 300,
    settings: {
      ...forceAtlas2.inferSettings(graph),
      ...SETTINGS,
      barnesHutOptimize: graph.order > 500,
    },
    getEdgeWeight: "weight",
  });
  const placed = new Map<string, [number, number]>();
  graph.forEachNode((key, attributes) => {
    placed.set(key, [attributes.x as number, attributes.y as number]);
  });
  const xs = [...placed.values()].map(([x]) => x);
  const ys = [...placed.values()].map(([, y]) => y);
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)) || 1;
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2;
  const cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  return {
    ...deps,
    nodes: deps.nodes.map((node) => {
      const [x, y] = placed.get(node.id) ?? [node.x, node.y];
      return { ...node, x: ((x - cx) * 2) / span, y: ((y - cy) * 2) / span };
    }),
  };
}
