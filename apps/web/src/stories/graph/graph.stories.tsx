import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, fn } from "storybook/test";

import { GraphLegend } from "@/components/graph/graph-legend";
import { GraphTable } from "@/components/graph/graph-table";
import type { DepEdge, ShownNode } from "@/lib/api/deps";

const NODES: ShownNode[] = [
  {
    id: "backend/app",
    label: "backend/app",
    layer: "db",
    size: 40,
    cluster: "backend",
    x: 0,
    y: 0,
    level: "module",
  },
  {
    id: "backend/app/api",
    label: "backend/app/api",
    layer: "route",
    size: 12,
    cluster: "backend",
    x: 0.5,
    y: 0,
    level: "module",
  },
  {
    id: "backend/app/core",
    label: "backend/app/core",
    layer: "service",
    size: 9,
    cluster: "backend",
    x: 0,
    y: 0.5,
    level: "module",
  },
];
const EDGES: DepEdge[] = [
  {
    source: "backend/app/api",
    target: "backend/app",
    weight: 84,
    types: { calls: 70, imports: 14 },
    confidence: 0.95,
    samples: [],
  },
  {
    source: "backend/app",
    target: "backend/app/core",
    weight: 46,
    types: { imports: 46 },
    confidence: 0.95,
    samples: [],
  },
  {
    source: "backend/app/core",
    target: "backend/app",
    weight: 8,
    types: { calls: 8 },
    confidence: 0.9,
    samples: [],
  },
];

const meta = {
  title: "Graph/Explorer",
  parameters: { layout: "padded" },
} satisfies Meta;

export default meta;

export const Table: StoryObj<typeof GraphTable> = {
  render: (args) => <GraphTable {...args} />,
  args: { nodes: NODES, edges: EDGES, onSelectNode: fn() },
  play: async ({ args, canvas, userEvent }) => {
    const nodes = canvas.getByRole("table", { name: /Nodes, most depended on first/ });
    // The most depended on first: backend/app (84 + 8).
    const first = nodes.querySelectorAll("tbody tr")[0];
    await expect(first).toHaveTextContent("backend/app");
    await expect(first).toHaveTextContent("92");
    const [core] = canvas.getAllByRole("button", { name: "backend/app/core" });
    if (core) await userEvent.click(core);
    await expect(args.onSelectNode).toHaveBeenCalledWith("backend/app/core");
    await expect(canvas.getByRole("table", { name: /Edges, heaviest first/ })).toHaveTextContent(
      "70 calls, 14 imports",
    );
  },
};

export const Legend: StoryObj<typeof GraphLegend> = {
  render: () => <GraphLegend />,
  play: async ({ canvas }) => {
    const legend = canvas.getByRole("list", { name: "Legend" });
    for (const label of ["UI", "API", "Service", "Data", "Other", "Closes a cycle"]) {
      await expect(legend).toHaveTextContent(label);
    }
  },
};
