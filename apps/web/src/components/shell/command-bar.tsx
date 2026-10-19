"use client";

import { Dialog as DialogPrimitive } from "@base-ui/react/dialog";
import {
  Activity,
  House,
  Inbox,
  Keyboard,
  LogOut,
  MessageSquare,
  PanelLeft,
  Settings,
  SunMoon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import type { ReactNode } from "react";

import { KeyHint } from "@/components/primitives/key-hint";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command";
import { orgPath, type OrgSection } from "@/lib/routes";
import { shortcut, type ShortcutId } from "@/lib/shortcut-registry";

import { useShellStore } from "./shell-store";
import { signOut } from "./sign-out";
import { useOrgSlug } from "./use-org-slug";

interface PaletteItem {
  label: string;
  icon: ReactNode;
  run: () => void;
  shortcut?: ShortcutId;
  disabled?: boolean;
}

/**
 * The ⌘K palette. Opens with no animation (it's used constantly) and returns focus to whatever
 * had it before opening.
 */
export function CommandBar() {
  const org = useOrgSlug();
  const router = useRouter();
  const { resolvedTheme, setTheme } = useTheme();
  const open = useShellStore((state) => state.commandOpen);
  const setOpen = useShellStore((state) => state.setCommandOpen);
  const toggleSidebar = useShellStore((state) => state.toggleSidebar);
  const setShortcutsOpen = useShellStore((state) => state.setShortcutsOpen);

  const close = () => {
    setOpen(false);
  };
  const go = (section: OrgSection) => () => {
    router.push(orgPath(org, section));
  };

  const groups: { heading: string; items: PaletteItem[] }[] = [
    {
      heading: "Go",
      items: [
        { label: "Home", icon: <House />, run: go(""), shortcut: "go-home" },
        { label: "Inbox", icon: <Inbox />, run: go("inbox"), shortcut: "go-inbox" },
        { label: "Activity", icon: <Activity />, run: go("activity"), shortcut: "go-activity" },
        { label: "Settings", icon: <Settings />, run: go("settings"), shortcut: "go-settings" },
      ],
    },
    {
      heading: "Do",
      items: [
        {
          label: resolvedTheme === "light" ? "Switch to dark theme" : "Switch to light theme",
          icon: <SunMoon />,
          run: () => {
            setTheme(resolvedTheme === "light" ? "dark" : "light");
          },
        },
        {
          label: "Toggle the sidebar",
          icon: <PanelLeft />,
          run: toggleSidebar,
          shortcut: "toggle-sidebar",
        },
        {
          label: "Show keyboard shortcuts",
          icon: <Keyboard />,
          run: () => {
            setShortcutsOpen(true);
          },
          shortcut: "shortcuts",
        },
        { label: "Sign out", icon: <LogOut />, run: () => void signOut() },
      ],
    },
    {
      heading: "Ask",
      items: [
        // Enabled once a repository is indexed (P2).
        { label: "Ask about this codebase", icon: <MessageSquare />, run: close, disabled: true },
      ],
    },
  ];

  return (
    // Base UI gives focus back to whatever had it before opening (also when opened by ⌘K).
    <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Backdrop className="fixed inset-0 z-50 bg-black/30" />
        <DialogPrimitive.Popup
          aria-label="Command palette"
          className="fixed top-[18vh] left-1/2 z-50 w-[min(640px,calc(100%-2rem))] -translate-x-1/2 overflow-hidden rounded-xl border border-line-strong glass shadow-dialog outline-none"
        >
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <Command className="bg-transparent" loop>
            <CommandInput placeholder="Go to, do, or ask…" aria-label="Command" />
            <CommandList className="max-h-[50vh]">
              {groups.map((group) => (
                <CommandGroup key={group.heading} heading={group.heading}>
                  {group.items.map((item) => (
                    <CommandItem
                      key={item.label}
                      disabled={item.disabled === true}
                      onSelect={() => {
                        close();
                        item.run();
                      }}
                    >
                      <span aria-hidden className="[&_svg]:size-4">
                        {item.icon}
                      </span>
                      {item.label}
                      {item.shortcut && (
                        <CommandShortcut>
                          <KeyHint shortcut={shortcut(item.shortcut).keys} />
                        </CommandShortcut>
                      )}
                    </CommandItem>
                  ))}
                </CommandGroup>
              ))}
            </CommandList>
            <CommandEmpty>No matches.</CommandEmpty>
          </Command>
        </DialogPrimitive.Popup>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
