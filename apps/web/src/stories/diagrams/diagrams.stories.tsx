import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, fn, userEvent, waitFor } from "storybook/test";

import { BlueprintCard } from "@/components/diagrams/blueprint-node";
import { DiagramCanvas } from "@/components/diagrams/diagram-canvas";
import type { Spec } from "@/lib/api/diagrams";
import { appArchitecture, appSchema } from "@/mocks/app-architecture";

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
      members: [
        { name: "useAuth", kind: "hook" },
        { name: "useForm", kind: "hook" },
      ],
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
    {
      id: "table",
      label: "user",
      layer: "db",
      kind: "table",
      citation: null,
      members: [
        { name: "id", kind: "column" },
        { name: "email", kind: "column" },
        { name: "hashed_password", kind: "column" },
      ],
      more_members: 3,
    },
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

/** A flowchart reads top to bottom: each step numbered in reading order, its start and end said. */
export const FlowchartSteps: Story = {
  args: { view: "flowchart" },
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getAllByTestId("blueprint-node")).toHaveLength(5), {
      timeout: 5000,
    });
    const steps = canvas.getAllByText("Step").map((word) => word.parentElement?.textContent);
    await expect(steps.sort()).toEqual(["Step 1", "Step 2", "Step 3", "Step 4", "Step 5"]);
    // React Flow shows a node once it has measured it.
    await waitFor(() => expect(canvas.getByText("End")).toBeVisible(), { timeout: 5000 });
  },
};

/** The same diagram as a sequence: participants across, lifelines down, messages numbered in order. */
export const Sequence: Story = {
  args: { view: "sequence" },
  play: async ({ canvas, args }) => {
    await expect(canvas.getByTestId("sequence-view")).toBeVisible();
    await expect(canvas.getAllByTestId("blueprint-node")).toHaveLength(5);
    const messages = canvas
      .getByTestId("sequence-view")
      .querySelectorAll('[data-testid="sequence-message"]');
    await expect(messages).toHaveLength(5);
    await expect(messages[4]?.classList.contains("inferred")).toBe(true);
    await userEvent.click(canvas.getByRole("button", { name: /\buser\b/ }));
    await expect(args.onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: "table" }));
  },
};

/** The same diagram as a system: a row per layer, top to bottom, the table a cylinder. */
export const ArchitectureSystem: Story = {
  args: { view: "architecture" },
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getAllByTestId("system-node")).toHaveLength(5), {
      timeout: 5000,
    });
    await expect(canvas.getAllByTestId("system-row").map((row) => row.textContent)).toEqual([
      "Frontend",
      "API",
      "Services",
      "Data",
    ]);
    const table = canvas.getAllByTestId("system-node").find((n) => n.dataset.store === "true");
    await expect(table).toHaveTextContent("user");
  },
};

const WHOLE_APP = appArchitecture("Architecture of fastapi/full-stack-fastapi-template");
const SCHEMA = appSchema("Schema of fastapi/full-stack-fastapi-template");

/** A whole app's architecture, as the API draws it: its users, its parts named, the systems
 * outside it. */
export const WholeApp: Story = {
  args: { spec: WHOLE_APP, edges: WHOLE_APP.edges, mermaid: "" },
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getAllByTestId("system-node")).toHaveLength(9), {
      timeout: 5000,
    });
    // React Flow shows a node once it has measured it.
    await waitFor(() => expect(canvas.getByText("Auth & security")).toBeVisible(), {
      timeout: 5000,
    });
    await expect(canvas.getByText("Settings, password hashing and access tokens.")).toBeVisible();
  },
};

/** A schema as an ER diagram: tables with their keys, types and the relation between rows. */
export const EntityRelationship: Story = {
  args: { spec: SCHEMA, edges: SCHEMA.edges, mermaid: "" },
  play: async ({ canvas }) => {
    await waitFor(() => expect(canvas.getAllByTestId("table-node")).toHaveLength(2), {
      timeout: 5000,
    });
    const item = await waitFor(() => canvas.getByRole("list", { name: "item's columns" }), {
      timeout: 5000,
    });
    await expect(item).toHaveTextContent("owner_id");
    await expect(item).toHaveTextContent("a foreign key to");
    await expect(canvas.getByRole("list", { name: "user's columns" })).toHaveTextContent(
      "Primary key id",
    );
  },
};
