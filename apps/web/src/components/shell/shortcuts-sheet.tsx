"use client";

import { cn } from "cn";

import { KeyHint } from "@/components/primitives/key-hint";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { SHORTCUTS, type ShortcutGroup } from "@/lib/shortcut-registry";

import { useShellStore } from "./shell-store";

const GROUPS: ShortcutGroup[] = ["General", "Navigation", "View"];

export function ShortcutsSheet() {
  const open = useShellStore((state) => state.shortcutsOpen);
  const setOpen = useShellStore((state) => state.setShortcutsOpen);
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent side="right" className="w-96">
        <SheetHeader>
          <SheetTitle>Keyboard shortcuts</SheetTitle>
          <SheetDescription>Sequences like “g h” mean: press g, then h.</SheetDescription>
        </SheetHeader>
        <ShortcutList className="px-4 pb-6" />
      </SheetContent>
    </Sheet>
  );
}

/** Every shortcut by group (``filter``: only those whose words or keys have it), as the sheet
 * and the shortcuts settings page show them. */
export function ShortcutList({
  filter = "",
  headingLevel = 3,
  className,
}: {
  filter?: string;
  /** Its groups' heading level: under the page's or the sheet's title. */
  headingLevel?: 2 | 3;
  className?: string;
}) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const words = filter.trim().toLowerCase();
  const shown = SHORTCUTS.filter(
    (item) => !words || item.label.toLowerCase().includes(words) || item.keys.includes(words),
  );
  if (shown.length === 0) {
    return <p className="text-sm text-fg-faint">No shortcut matches that.</p>;
  }
  return (
    <div className={cn("flex flex-col gap-6", className)}>
      {GROUPS.filter((group) => shown.some((item) => item.group === group)).map((group) => (
        <section key={group} aria-labelledby={`shortcuts-${group}`}>
          <Heading id={`shortcuts-${group}`} className="mb-2 text-xs font-medium text-fg-faint">
            {group}
          </Heading>
          <dl className="flex flex-col divide-y divide-line">
            {shown
              .filter((item) => item.group === group)
              .map((item) => (
                <div key={item.id} className="flex items-center justify-between py-2 text-sm">
                  <dt>{item.label}</dt>
                  <dd>
                    <KeyHint shortcut={item.keys} />
                  </dd>
                </div>
              ))}
          </dl>
        </section>
      ))}
    </div>
  );
}
