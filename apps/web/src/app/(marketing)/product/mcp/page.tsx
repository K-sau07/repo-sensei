import type { Metadata } from "next";

import { McpSetups } from "@/components/docs/mcp-setups";
import { ProductPage } from "@/components/marketing/product/product-page";

const LEAD =
  "Your coding agent reads the codebase through the same index: search, symbols, code, traces, the graph, impact and answers, over MCP.";

export const metadata: Metadata = {
  title: "MCP",
  description: LEAD,
  alternates: { canonical: "/product/mcp" },
};

const POINTS = [
  "Sign in once with OAuth from the editor, or run it locally with an API key.",
  "Every tool only reads, and only the repositories you can read.",
  "What it returns from the code is marked as the repository's, not instructions to follow.",
  "The sensei command line does the same from a terminal.",
] as const;

const TOOLS: readonly [string, string][] = [
  ["sensei_list_repos", "The repositories you can read."],
  ["sensei_search", "Exact, keyword and meaning search."],
  ["sensei_find_symbol", "Where a symbol is defined."],
  ["sensei_read_code", "A file's lines, numbered."],
  ["sensei_trace", "A feature, hop by hop across layers."],
  ["sensei_graph_neighbors", "What a symbol calls and what calls it."],
  ["sensei_impact", "What a change to a symbol reaches."],
  ["sensei_ask", "An answer with its citations."],
];

export default function McpPage() {
  return (
    <ProductPage
      title="MCP"
      lead={LEAD}
      points={POINTS}
      example="Connect it: the setup for each editor"
    >
      <McpSetups />
      <h3 className="mt-4 text-sm font-medium text-fg-muted">Its tools</h3>
      <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
        {TOOLS.map(([name, what]) => (
          <div key={name} className="flex flex-col">
            <dt className="font-mono text-xs">{name}</dt>
            <dd className="text-fg-muted">{what}</dd>
          </div>
        ))}
      </dl>
    </ProductPage>
  );
}
