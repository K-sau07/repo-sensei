import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { useState } from "react";
import { expect, fn, userEvent, within } from "storybook/test";

import { Composer, NO_SCOPE } from "@/components/ask/composer";
import type { AskMode, ScopeData } from "@/lib/api/ask";
import { handlers } from "@/mocks/handlers";

const navigation = {
  appDirectory: true,
  navigation: {
    pathname: "/acme/api/ask",
    segments: [
      ["org", "acme"],
      ["repo", "api"],
    ],
  },
};

function Box({
  start = NO_SCOPE,
  onSend,
}: {
  start?: ScopeData;
  onSend: (t: string, s: ScopeData | null) => void;
}) {
  const [scope, setScope] = useState(start);
  const [mode, setMode] = useState<AskMode>("fast");
  return (
    <div className="max-w-2xl pt-72">
      <Composer
        draftKey="story-composer"
        busy={false}
        mode={mode}
        onMode={setMode}
        scope={scope}
        onScope={setScope}
        onSend={onSend}
        onStop={() => undefined}
      />
    </div>
  );
}

const meta = {
  title: "Ask/Composer",
  component: Box,
  parameters: { nextjs: navigation, msw: handlers },
  args: { onSend: fn() },
  beforeEach: () => {
    localStorage.removeItem("story-composer");
  },
} satisfies Meta<typeof Box>;

export default meta;
type Story = StoryObj<typeof meta>;

/** `@` lists files and symbols as it is typed; Enter writes the picked one in backticks. */
export const Mention: Story = {
  play: async ({ canvas }) => {
    const box = canvas.getByRole("textbox", { name: "Ask about this repository" });
    await userEvent.type(box, "what calls @create_it");
    const list = await canvas.findByRole("listbox", { name: "Mention a file or symbol" });
    await expect(within(list).getAllByRole("option").length).toBeGreaterThan(0);
    await userEvent.keyboard("{Enter}");
    // The raw value (the matcher's leaves out the trailing space): the name, then a space.
    await expect((box as HTMLTextAreaElement).value).toMatch(/^what calls `[\w./-]+` $/);
  },
};

/** A question scoped to a folder and two layers: chips above the box, sent with it. */
export const Scoped: Story = {
  args: {
    start: { path: "backend/app", startLine: null, endLine: null, layers: ["api", "db"] },
  },
  play: async ({ canvas, args }) => {
    const chips = canvas.getAllByTestId("scope-chip");
    await expect(chips.map((chip) => chip.textContent)).toEqual(["app/", "API", "Data"]);
    await userEvent.click(canvas.getByRole("button", { name: "Remove the scope API" }));
    await userEvent.type(
      canvas.getByRole("textbox", { name: "Ask about this repository" }),
      "Where is it saved?{Meta>}{Enter}{/Meta}",
    );
    await expect(args.onSend).toHaveBeenCalledWith("Where is it saved?", {
      path: "backend/app",
      startLine: null,
      endLine: null,
      layers: ["db"],
    });
  },
};
