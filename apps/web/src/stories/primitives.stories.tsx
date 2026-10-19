import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { useState } from "react";
import { expect, screen, userEvent, waitFor } from "storybook/test";

import { ConfidenceBadge } from "@/components/primitives/confidence-badge";
import { CopyButton } from "@/components/primitives/copy-button";
import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { KeyHint } from "@/components/primitives/key-hint";
import { LAYERS, LayerBadge, type Layer } from "@/components/primitives/layer-badge";
import { LiveDot } from "@/components/primitives/live-dot";
import { ProvenanceTag } from "@/components/primitives/provenance-tag";
import { RelativeTime } from "@/components/primitives/relative-time";
import { Truncate } from "@/components/primitives/truncate";
import { Button } from "@/components/ui/button";

const NOW = new Date("2026-10-18T12:00:00Z");

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="w-32 text-sm text-fg-muted">{label}</span>
      {children}
    </div>
  );
}

function SpinnerDemo() {
  const [busy, setBusy] = useState(false);
  return (
    <>
      <Button
        variant="outline"
        size="sm"
        onClick={() => {
          setBusy((value) => !value);
        }}
      >
        {busy ? "Stop" : "Start work"}
      </Button>
      <DelayedSpinner active={busy} label="Working" />
    </>
  );
}

function Primitives() {
  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <Row label="KeyHint">
        <KeyHint shortcut="mod+k" platform="apple" />
        <KeyHint shortcut="mod+k" platform="other" />
        <KeyHint shortcut="shift+mod+p" platform="apple" />
        <KeyHint shortcut="g r" platform="other" />
        <KeyHint shortcut="?" />
      </Row>
      <Row label="LiveDot">
        <span className="inline-flex items-center gap-2 text-sm">
          <LiveDot label="Indexing" /> Indexing
        </span>
      </Row>
      <Row label="LayerBadge">
        {(Object.keys(LAYERS) as Layer[]).map((layer) => (
          <LayerBadge key={layer} layer={layer} />
        ))}
        {(Object.keys(LAYERS) as Layer[]).map((layer) => (
          <LayerBadge key={`${layer}-compact`} layer={layer} compact />
        ))}
      </Row>
      <Row label="ProvenanceTag">
        <ProvenanceTag provenance="precise" />
        <ProvenanceTag provenance="heuristic" />
        <ProvenanceTag provenance="inferred" />
      </Row>
      <Row label="ConfidenceBadge">
        <ConfidenceBadge confidence="high" />
        <ConfidenceBadge confidence="medium" />
        <ConfidenceBadge confidence="low" />
      </Row>
      <Row label="CopyButton">
        <code className="font-mono text-sm">01a1523c-c456-7493</code>
        <CopyButton value="01a1523c-c456-7493" label="Copy request ID" />
      </Row>
      <Row label="RelativeTime">
        <RelativeTime date={new Date(NOW.getTime() - 3 * 60_000)} now={NOW} className="text-sm" />
        <RelativeTime
          date={new Date(NOW.getTime() - 26 * 3_600_000)}
          now={NOW}
          className="text-sm"
        />
      </Row>
      <Row label="Truncate">
        <div className="w-56 text-sm">
          <Truncate text="src/backend/base/langflow/api/v1/endpoints/flows/build_vertices.py" />
        </div>
      </Row>
      <Row label="DelayedSpinner">
        <SpinnerDemo />
      </Row>
    </div>
  );
}

const meta = { title: "Primitives/All", component: Primitives } satisfies Meta<typeof Primitives>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dark: Story = {
  play: async ({ canvas }) => {
    await expect(canvas.getByRole("img", { name: "Shift Command P" })).toBeInTheDocument();
    await expect(canvas.getByText("3 minutes ago")).toBeInTheDocument();
    await expect(canvas.getByText("yesterday")).toBeInTheDocument();

    // The spinner waits 200ms before appearing.
    await userEvent.click(canvas.getByRole("button", { name: "Start work" }));
    await expect(canvas.queryByText("Working")).toBeNull();
    await waitFor(() => expect(canvas.getByText("Working")).toBeInTheDocument());

    await userEvent.hover(canvas.getByRole("button", { name: "Inferred" }));
    await expect(
      await screen.findByText(/Suggested by the model/, {}, { timeout: 2000 }),
    ).toBeInTheDocument();
  },
};

export const Light: Story = { globals: { theme: "light" } };
