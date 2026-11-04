import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, userEvent, waitFor } from "storybook/test";

import { AppearanceView } from "@/components/settings/appearance-view";
import { ShortcutsView } from "@/components/settings/shortcuts-view";
import { APPEARANCE_STYLE_ID, DEFAULT_APPEARANCE, saveAppearance } from "@/lib/appearance";

const meta = {
  title: "Settings/Appearance",
  parameters: {
    nextjs: { appDirectory: true, navigation: { pathname: "/settings/appearance" } },
  },
  beforeEach: () => {
    saveAppearance(DEFAULT_APPEARANCE);
    return () => {
      saveAppearance(DEFAULT_APPEARANCE); // the next story starts from the defaults
    };
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const root = () => document.documentElement;
const palette = () => document.getElementById(APPEARANCE_STYLE_ID);

/** Each choice applies at once: an accent writes the palette, compact tightens the page,
 * reduced motion and transparency mark the document; the defaults undo it all. */
export const Appearance: Story = {
  render: () => <AppearanceView />,
  play: async ({ canvas }) => {
    await expect(palette()).toBeNull();
    await userEvent.click(canvas.getByRole("radio", { name: /Green/ }));
    await waitFor(async () => {
      await expect(palette()?.textContent).toContain("--brand:");
    });
    await userEvent.click(canvas.getByRole("radio", { name: /Compact/ }));
    await expect(root().dataset.density).toBe("compact");
    await userEvent.click(canvas.getByRole("switch", { name: "Reduce motion" }));
    await expect(root().dataset.motion).toBe("reduce");
    await userEvent.click(canvas.getByRole("switch", { name: "Reduce transparency" }));
    await expect(root().dataset.transparency).toBe("reduce");
    await userEvent.click(canvas.getByRole("button", { name: "Back to the defaults" }));
    await expect(palette()).toBeNull();
    await expect(root().dataset.density).toBe("comfortable");
    await expect(canvas.getByRole("switch", { name: "Reduce motion" })).not.toBeChecked();
  },
};

/** Every shortcut, found by its words or its keys. */
export const Shortcuts: Story = {
  render: () => <ShortcutsView />,
  play: async ({ canvas }) => {
    await expect(canvas.getByText("Open the command palette")).toBeVisible();
    await userEvent.type(canvas.getByLabelText("Find a shortcut"), "inbox");
    await expect(canvas.getByText("Go to inbox")).toBeVisible();
    await expect(canvas.queryByText("Open the command palette")).toBeNull();
    await userEvent.clear(canvas.getByLabelText("Find a shortcut"));
    await userEvent.type(canvas.getByLabelText("Find a shortcut"), "nothing like this");
    await expect(canvas.getByText("No shortcut matches that.")).toBeVisible();
  },
};
