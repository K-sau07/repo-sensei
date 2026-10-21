import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { useState } from "react";
import { expect, userEvent } from "storybook/test";

import { KeepAlive } from "@/components/primitives/keep-alive";
import { Button } from "@/components/ui/button";

function Counter({ name }: { name: string }) {
  const [count, setCount] = useState(0);
  const [text, setText] = useState("");
  return (
    <div className="flex flex-col gap-2">
      <Button
        variant="outline"
        onClick={() => {
          setCount((value) => value + 1);
        }}
      >
        {name}: {count}
      </Button>
      <input
        aria-label={`${name} note`}
        className="rounded-sm border border-line bg-transparent px-2 py-1"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
        }}
      />
    </div>
  );
}

function Tabs() {
  const [active, setActive] = useState("first");
  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-2">
        {["first", "second"].map((tab) => (
          <Button
            key={tab}
            variant={tab === active ? "default" : "ghost"}
            onClick={() => {
              setActive(tab);
            }}
          >
            Show {tab}
          </Button>
        ))}
      </div>
      <KeepAlive
        active={active}
        panels={{
          first: () => <Counter name="First" />,
          second: () => <Counter name="Second" />,
        }}
      />
    </div>
  );
}

const meta = {
  title: "Primitives/Keep alive",
  component: Tabs,
} satisfies Meta<typeof Tabs>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A panel's state (a counter, typed text) survives switching away and back. */
export const StateSurvivesSwitching: Story = {
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: "First: 0" }));
    await userEvent.type(canvas.getByLabelText("First note"), "keep me");
    await userEvent.click(canvas.getByRole("button", { name: "Show second" }));
    await expect(canvas.queryByRole("button", { name: /First:/ })).toBeNull(); // hidden
    await expect(canvas.getByRole("button", { name: "Second: 0" })).toBeVisible();
    await userEvent.click(canvas.getByRole("button", { name: "Show first" }));
    await expect(canvas.getByRole("button", { name: "First: 1" })).toBeVisible();
    await expect(canvas.getByLabelText("First note")).toHaveValue("keep me");
  },
};
