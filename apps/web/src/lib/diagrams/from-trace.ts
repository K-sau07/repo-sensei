/**
 * A diagram drawn straight from a trace (the mock API's diagrams, 6.6, and the product page's
 * example, 10.3): its hops as the nodes, its edges as the trace found them, the Mermaid the API
 * would compile for it, and the share of its edges that are verified.
 */
import type { Spec, SpecEdge, SpecNode } from "@/lib/api/diagrams";
import type { Trace } from "@/lib/api/trace";

/** A flowchart's Mermaid, as the API compiles it. */
export function mermaid(spec: Spec): string {
  const ids = new Map(spec.nodes.map((node, i) => [node.id, `n${String(i + 1)}`]));
  const label = (text: string) => text.replace(/["`<>{}[\]|#;]/g, " ").trim() || "?";
  const lines = [spec.kind === "sequence" ? "sequenceDiagram" : "flowchart LR"];
  if (spec.kind === "sequence") {
    for (const node of spec.nodes)
      lines.push(`  participant ${ids.get(node.id) ?? "?"} as ${label(node.label)}`);
    for (const edge of spec.edges) {
      const arrow = edge.verified ? "->>" : "-->>";
      lines.push(
        `  ${ids.get(edge.from) ?? "?"}${arrow}${ids.get(edge.to) ?? "?"}: ${label(edge.label ?? edge.type ?? "")}`,
      );
    }
    return `${lines.join("\n")}\n`;
  }
  for (const node of spec.nodes) lines.push(`  ${ids.get(node.id) ?? "?"}["${label(node.label)}"]`);
  for (const edge of spec.edges) {
    const arrow = edge.verified ? "-->" : "-.->";
    const text = edge.label ? `|${label(edge.label)}|` : "";
    lines.push(`  ${ids.get(edge.from) ?? "?"} ${arrow}${text} ${ids.get(edge.to) ?? "?"}`);
  }
  return `${lines.join("\n")}\n`;
}

/** The spec of a trace's first path: its hops (each once) and the edges along it (each the
 * trace's own: verified). `inferred` adds one edge the graph can't show, as the validator keeps
 * those (the mock's, for a diagram asked by question). */
export function specOfTrace(
  trace: Trace,
  title: string,
  kind: Spec["kind"],
  inferred: boolean,
): Spec {
  const path = trace.paths[0];
  const nodes: SpecNode[] = [];
  const seen = new Set<string>();
  const edges: SpecEdge[] = [];
  let previous: string | null = null;
  for (const hop of path?.hops ?? []) {
    const key = hop.node.symbol_key;
    if (!seen.has(key)) {
      seen.add(key);
      nodes.push({
        id: key,
        label: hop.node.name,
        layer: hop.layer,
        kind: hop.node.kind,
        citation:
          hop.node.path && hop.node.lines
            ? { path: hop.node.path, start_line: hop.node.lines[0], end_line: hop.node.lines[1] }
            : null,
      });
    }
    if (previous !== null && hop.edge && previous !== key) {
      edges.push({
        from: previous,
        to: key,
        type: hop.edge.type,
        label: hop.edge.type.replaceAll("_", " "),
        verified: true,
        confidence: hop.edge.confidence,
        provenance: hop.edge.provenance,
        inferred: null,
      });
    }
    previous = key;
  }
  const first = nodes[0];
  const last = nodes.at(-1);
  if (inferred && first && last && first.id !== last.id) {
    edges.push({
      from: last.id,
      to: first.id,
      type: null,
      label: "signs the user in",
      verified: false,
      confidence: null,
      provenance: null,
      inferred: { reason: "the session is set by middleware the graph can't see" },
    });
  }
  return { title, kind, nodes, edges };
}

export function ratio(spec: Spec): number {
  return spec.edges.length ? spec.edges.filter((e) => e.verified).length / spec.edges.length : 1;
}
