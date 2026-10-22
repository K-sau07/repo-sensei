"use client";

import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";

import { CommandPalette } from "@/components/palette/command-palette";

import { useShellStore } from "./shell-store";

/**
 * The ⌘K palette. Opens with no animation (it's used constantly) and returns focus to whatever
 * had it before opening. A glass panel on wide screens; the whole screen on a phone.
 */
export function CommandBar() {
  const open = useShellStore((state) => state.commandOpen);
  const setOpen = useShellStore((state) => state.setCommandOpen);

  return (
    // Base UI gives focus back to whatever had it before opening (also when opened by ⌘K).
    <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Backdrop className="fixed inset-0 z-50 bg-black/30" />
        <DialogPrimitive.Popup
          aria-label="Command palette"
          className="fixed inset-0 z-50 flex flex-col overflow-hidden glass outline-none sm:inset-auto sm:top-[18vh] sm:left-1/2 sm:w-[min(640px,calc(100%-2rem))] sm:-translate-x-1/2 sm:rounded-xl sm:border sm:border-line-strong sm:shadow-dialog"
        >
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <CommandPalette
            onClose={() => {
              setOpen(false);
            }}
          />
          {/* A phone keyboard has no Escape. */}
          <DialogPrimitive.Close className="m-2 h-control-md rounded-sm border border-line text-sm text-fg-muted hover:bg-surface-3 hover:text-fg focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none sm:hidden">
            Close
          </DialogPrimitive.Close>
        </DialogPrimitive.Popup>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
