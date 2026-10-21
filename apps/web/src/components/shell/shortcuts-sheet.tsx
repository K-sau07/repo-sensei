"use client";

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
        <div className="flex flex-col gap-6 px-4 pb-6">
          {GROUPS.map((group) => (
            <section key={group} aria-labelledby={`shortcuts-${group}`}>
              <h3 id={`shortcuts-${group}`} className="mb-2 text-xs font-medium text-fg-faint">
                {group}
              </h3>
              <dl className="flex flex-col divide-y divide-line">
                {SHORTCUTS.filter((item) => item.group === group).map((item) => (
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
      </SheetContent>
    </Sheet>
  );
}
