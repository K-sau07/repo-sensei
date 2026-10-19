import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { FileCode, MessageSquare, Waypoints } from "lucide-react";
import { expect, userEvent } from "storybook/test";

import { KeyHint } from "@/components/primitives/key-hint";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command";

function Palette() {
  return (
    <Command className="max-w-lg rounded-xl border border-line shadow-overlay">
      <CommandInput placeholder="Search files, symbols, commands…" aria-label="Command" />
      <CommandList>
        <CommandGroup heading="Actions">
          <CommandItem>
            <MessageSquare aria-hidden />
            Ask a question
            <CommandShortcut>
              <KeyHint shortcut="mod+enter" platform="apple" />
            </CommandShortcut>
          </CommandItem>
          <CommandItem>
            <Waypoints aria-hidden />
            Trace from a route
          </CommandItem>
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Files">
          <CommandItem>
            <FileCode aria-hidden />
            src/api/routes/checkout.py
          </CommandItem>
        </CommandGroup>
      </CommandList>
      <CommandEmpty>No results.</CommandEmpty>
    </Command>
  );
}

const meta = { title: "UI/Command", component: Palette } satisfies Meta<typeof Palette>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Results: Story = {};
export const Light: Story = { globals: { theme: "light" } };

export const NoResults: Story = {
  play: async ({ canvas }) => {
    await userEvent.type(canvas.getByRole("combobox"), "zzzz-nothing");
    await expect(await canvas.findByText("No results.")).toBeVisible();
  },
};
