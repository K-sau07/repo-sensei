import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, fn, waitFor } from "storybook/test";

import { BlueprintCard } from "@/components/diagrams/blueprint-node";
import { DiagramCanvas } from "@/components/diagrams/diagram-canvas";
import type { Spec } from "@/lib/api/diagrams";

const SPEC: Spec = {
  title: "Signing in",
  kind: "flowchart",
  nodes: [
    {
      id: "page",
      label: "Login",
      layer: "ui",
      kind: "component",
      citation: { path: "frontend/src/routes/login.tsx", start_line: 12, end_line: 80 },
    },
    {
      id: "route",
      label: "POST /api/v1/login/access-token",
      layer: "route",
      kind: "api_endpoint",
      citation: null,
    },
    {
      id: "handler",
      label: "login_access_token",
      layer: "handler",
      kind: "function",
      citation: { path: "backend/app/api/routes/login.py", start_line: 21, end_line: 40 },
    },
    {
      id: "crud",
      label: "authenticate",
      layer: "service",
      kind: "function",
      citation: { path: "backend/app/crud.py", start_line: 52, end_line: 61 },
    },
    { id: "table", label: "user", layer: "db", kind: "table", citation: null },
  ],
  edges: [
    {
      from: "page",
      to: "route",
      type: "targets",
      label: "posts the form",
      verified: true,
      confidence: 0.9,
      provenance: "url_literal",
      inferred: null,
    },
    {
      from: "route",
      to: "handler",
      type: "handled_by",
      label: "handled by",
      verified: true,
      confidence: 1,
      provenance: "runtime",
      inferred: null,
    },
    {
      from: "handler",
      to: "crud",
      type: "calls",
      label: "checks the password",
      verified: true,
      confidence: 0.95,
      provenance: "import_resolved",
      inferred: null,
    },
    {
      from: "crud",
      to: "table",
      type: "reads",
      label: "reads the user",
      verified: true,
      confidence: 0.9,
      provenance: "import_resolved",
      inferred: null,
    },
    {
      from: "handler",
      to: "page",
      type: null,
      label: "returns a token",
      verified: false,
      confidence: null,
      provenance: null,
      inferred: { reason: "the response travels back over HTTP" },
    },
  ],
};

const meta = {
  title: "Diagrams/Canvas",
  component: DiagramCanvas,
  parameters: { layout: "fullscreen" },
  args: {
    spec: SPEC,
    edges: SPEC.edges,
    mermaid: "flowchart LR\n",
    title: SPEC.title,
    selected: null,
    onSelect: fn(),
  },
  decorators: [
    (Story) => (
      <div style={{ height: 520 }}>
        <Story />
      </div>
    ),
  ],
} satisfies Meta<typeof DiagramCanvas>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Laid out by ELK: the verified edges solid, the inferred one dashed amber, with its reason. */
export const Verified: Story = {
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getAllByTestId("blueprint-node")).toHaveLength(5), {
      timeout: 5000,
    });
    await waitFor(() =>
      expect(
        canvas.getByTestId("diagram-canvas").querySelectorAll(".react-flow__edge.inferred"),
      ).toHaveLength(1),
    );
    await expect(canvas.getByRole("toolbar", { name: "Diagram tools" })).toBeVisible();
  },
};

/** Only what the graph proves. */
export const HideUnverified: Story = {
  args: { edges: SPEC.edges.filter((e) => e.verified) },
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getAllByTestId("blueprint-node")).toHaveLength(5), {
      timeout: 5000,
    });
    await expect(
      canvas.getByTestId("diagram-canvas").querySelectorAll(".react-flow__edge.inferred"),
    ).toHaveLength(0);
  },
};

/** Drawn again: what is new is ringed. */
export const Regenerated: Story = {
  args: { added: new Set(["crud"]), selected: "handler" },
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getAllByTestId("blueprint-node")).toHaveLength(5), {
      timeout: 5000,
    });
    const added = canvas.getAllByTestId("blueprint-node").filter((n) => n.dataset.added);
    await expect(added).toHaveLength(1);
  },
};

/** The cards, one per layer: glyph, name and color, never color alone. */
export const Cards: Story = {
  render: () => (
    <div className="grid w-fit grid-cols-2 gap-3 p-6">
      {SPEC.nodes.map((node) => (
        <div key={node.id} style={{ width: 220, height: 64 }}>
          <BlueprintCard node={node} added={node.id === "crud"} />
        </div>
      ))}
    </div>
  ),
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Endpoint")).toBeVisible();
    await expect(canvas.getByText("crud.py:52–61")).toBeVisible();
  },
};
