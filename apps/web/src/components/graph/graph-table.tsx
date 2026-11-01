"use client";

/**
 * The graph as tables (6.7.5): its nodes (most depended on first) and its edges (heaviest
 * first), for a screen reader, a keyboard, or a browser without WebGL2. A node's name (and an
 * edge's ends) focuses it, as a click on the canvas does. A big level's first rows are listed.
 */
import { useMemo } from "react";

import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { typesLabel, type DepEdge, type ShownNode } from "@/lib/api/deps";

export const TABLE_ROWS = 500;

export function GraphTable({
  nodes,
  edges,
  onSelectNode,
}: {
  nodes: readonly ShownNode[];
  edges: readonly DepEdge[];
  onSelectNode: (id: string) => void;
}) {
  const rows = useMemo(() => {
    const into = new Map<string, number>();
    const out = new Map<string, number>();
    for (const edge of edges) {
      into.set(edge.target, (into.get(edge.target) ?? 0) + edge.weight);
      out.set(edge.source, (out.get(edge.source) ?? 0) + edge.weight);
    }
    return nodes
      .map((node) => ({ node, into: into.get(node.id) ?? 0, out: out.get(node.id) ?? 0 }))
      .sort((a, b) => b.into - a.into || a.node.label.localeCompare(b.node.label));
  }, [nodes, edges]);
  const labels = useMemo(() => new Map(nodes.map((n) => [n.id, n.label])), [nodes]);
  const heaviest = useMemo(() => [...edges].sort((a, b) => b.weight - a.weight), [edges]);
  return (
    <div className="flex size-full flex-col gap-6 overflow-auto p-4" data-testid="graph-table">
      <Table>
        <TableCaption>
          Nodes, most depended on first
          {rows.length > TABLE_ROWS
            ? ` (the first ${String(TABLE_ROWS)} of ${String(rows.length)})`
            : ""}
        </TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Layer</TableHead>
            <TableHead className="text-right">Depended on</TableHead>
            <TableHead className="text-right">Depends on</TableHead>
            <TableHead className="text-right">Definitions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.slice(0, TABLE_ROWS).map(({ node, into, out }) => (
            <TableRow key={node.id}>
              <TableCell>
                <button
                  type="button"
                  className="text-left underline-offset-4 hover:underline"
                  onClick={() => {
                    onSelectNode(node.id);
                  }}
                >
                  {node.label}
                </button>
              </TableCell>
              <TableCell>{node.layer}</TableCell>
              <TableCell className="text-right tabular-nums">{into}</TableCell>
              <TableCell className="text-right tabular-nums">{out}</TableCell>
              <TableCell className="text-right tabular-nums">{node.size}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Table>
        <TableCaption>
          Edges, heaviest first
          {heaviest.length > TABLE_ROWS
            ? ` (the first ${String(TABLE_ROWS)} of ${String(heaviest.length)})`
            : ""}
        </TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>From</TableHead>
            <TableHead>To</TableHead>
            <TableHead>What</TableHead>
            <TableHead className="text-right">Weight</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {heaviest.slice(0, TABLE_ROWS).map((edge) => (
            <TableRow key={`${edge.source}\u0000${edge.target}`}>
              {[edge.source, edge.target].map((end, i) => (
                <TableCell key={i}>
                  <button
                    type="button"
                    className="text-left underline-offset-4 hover:underline"
                    onClick={() => {
                      onSelectNode(end);
                    }}
                  >
                    {labels.get(end) ?? end}
                  </button>
                </TableCell>
              ))}
              <TableCell className="text-fg-muted">{typesLabel(edge.types)}</TableCell>
              <TableCell className="text-right tabular-nums">{edge.weight}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
