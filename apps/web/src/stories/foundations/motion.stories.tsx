import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import * as m from "motion/react-m";
import { useState } from "react";
import { expect, userEvent } from "storybook/test";

import { duration, durationMs, ease, type Duration } from "@/lib/motion/tokens";

const USES: Record<Duration, string> = {
  instant: "⌘K, keyboard navigation, context menus",
  fast: "hover, press, toggles",
  base: "popovers, dropdowns, tab content",
  slow: "panels, drawers, view transitions",
  signature: "Trace Beam, rare moments",
};

function MotionTokens() {
  const [moved, setMoved] = useState(false);
  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <div className="flex items-center gap-4">
        <button
          type="button"
          onClick={() => {
            setMoved((value) => !value);
          }}
          className="h-control-md rounded-sm bg-brand px-3 text-sm font-medium text-brand-fg transition-transform duration-(--dur-fast) ease-out active:scale-(--press-scale)"
        >
          Play
        </button>
        <p className="text-sm text-fg-muted">
          Only transform and opacity animate. With reduced motion, movement is skipped.
        </p>
      </div>
      <ul className="flex flex-col gap-4">
        {(Object.keys(durationMs) as Duration[]).map((name) => (
          <li key={name} className="flex items-center gap-4">
            <span className="w-44 text-sm">
              <span className="font-mono">--dur-{name}</span>{" "}
              <span className="text-fg-muted tabular-nums">{durationMs[name]}ms</span>
            </span>
            <div className="relative h-8 flex-1 rounded-sm bg-surface-1">
              <m.div
                data-testid={`box-${name}`}
                aria-hidden
                className="absolute top-1 left-1 size-6 rounded-sm bg-signal"
                animate={{ x: moved ? 240 : 0 }}
                transition={{ duration: duration[name], ease: ease.out }}
              />
            </div>
            <span className="w-56 text-xs text-fg-muted">{USES[name]}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

const meta = {
  title: "Foundations/Motion",
  component: MotionTokens,
} satisfies Meta<typeof MotionTokens>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Durations: Story = {
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Play" }));
    await expect(canvas.getByTestId("box-instant")).toBeInTheDocument();
  },
};
