import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, fireEvent, screen, userEvent, waitFor } from "storybook/test";

import { KeyHint } from "@/components/primitives/key-hint";
import { Button } from "@/components/ui/button";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuShortcut,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const meta = {
  title: "UI/Overlays",
  parameters: { layout: "fullscreen" },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

/** Overlays fade in from opacity 0, so wait for the enter animation before asserting. */
async function expectVisible(element: Promise<HTMLElement>) {
  const found = await element;
  await waitFor(() => expect(found).toBeVisible());
}

export const DialogOpen: Story = {
  render: () => (
    <Dialog>
      <DialogTrigger render={<Button variant="destructive" />}>Delete organization</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete Acme?</DialogTitle>
          <DialogDescription>
            Repositories, indexes and history are removed after a 7-day grace period.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
          <Button variant="destructive">Delete</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  ),
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Delete organization" }));
    await expectVisible(screen.findByRole("dialog", { name: "Delete Acme?" }));
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  },
};

export const DialogLight: Story = {
  ...DialogOpen,
  globals: { theme: "light" },
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Delete organization" }));
    await expectVisible(screen.findByRole("dialog"));
  },
};

export const SheetOpen: Story = {
  render: () => (
    <Sheet>
      <SheetTrigger render={<Button variant="outline" />}>Notifications</SheetTrigger>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Notifications</SheetTitle>
          <SheetDescription>Approvals, finished jobs and mentions.</SheetDescription>
        </SheetHeader>
      </SheetContent>
    </Sheet>
  ),
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Notifications" }));
    await expectVisible(screen.findByRole("dialog", { name: "Notifications" }));
  },
};

export const PopoverOpen: Story = {
  render: () => (
    <Popover>
      <PopoverTrigger render={<Button variant="outline" />}>Scope</PopoverTrigger>
      <PopoverContent>
        <PopoverHeader>
          <PopoverTitle>Search scope</PopoverTitle>
          <PopoverDescription>Limit answers to these paths and layers.</PopoverDescription>
        </PopoverHeader>
      </PopoverContent>
    </Popover>
  ),
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Scope" }));
    await expectVisible(screen.findByText("Search scope"));
  },
};

export const DropdownMenuOpen: Story = {
  render: () => (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="outline" />}>Account</DropdownMenuTrigger>
      <DropdownMenuContent className="w-56">
        <DropdownMenuGroup>
          <DropdownMenuLabel>octo@example.com</DropdownMenuLabel>
          <DropdownMenuItem>
            Settings
            <DropdownMenuShortcut>
              <KeyHint shortcut="mod+," platform="apple" />
            </DropdownMenuShortcut>
          </DropdownMenuItem>
          <DropdownMenuItem>Switch organization</DropdownMenuItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem variant="destructive">Sign out</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  ),
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Account" }));
    await expectVisible(screen.findByRole("menu"));
    await userEvent.keyboard("{ArrowDown}");
  },
};

export const ContextMenuOpen: Story = {
  render: () => (
    <ContextMenu>
      <ContextMenuTrigger className="flex h-32 w-72 items-center justify-center rounded-lg border border-dashed border-line-strong text-sm text-fg-muted">
        Right-click a symbol
      </ContextMenuTrigger>
      <ContextMenuContent className="w-56">
        <ContextMenuItem>
          Go to definition
          <ContextMenuShortcut>F12</ContextMenuShortcut>
        </ContextMenuItem>
        <ContextMenuItem>Find references</ContextMenuItem>
        <ContextMenuSeparator />
        <ContextMenuItem>Trace from here</ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  ),
  play: async ({ canvas }) => {
    await fireEvent.contextMenu(canvas.getByText("Right-click a symbol"));
    await expectVisible(screen.findByRole("menu"));
  },
};

export const TooltipOpen: Story = {
  render: () => (
    <div className="flex gap-2">
      {["Ask", "Search", "Trace"].map((label) => (
        <Tooltip key={label}>
          <TooltipTrigger render={<Button variant="ghost" size="sm" />}>{label}</TooltipTrigger>
          <TooltipContent>Open {label}</TooltipContent>
        </Tooltip>
      ))}
    </div>
  ),
  play: async ({ canvas }) => {
    await userEvent.hover(canvas.getByRole("button", { name: "Ask" }));
    // The first tooltip waits 400ms before opening.
    await expect(await screen.findByText("Open Ask", {}, { timeout: 2000 })).toBeInTheDocument();
  },
};
