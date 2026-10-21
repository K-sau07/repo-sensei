"use client";

import { Keyboard, LogOut, Moon, Sun, UserRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";

import { KeyHint } from "@/components/primitives/key-hint";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
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
import { shortcut } from "@/lib/shortcut-registry";

import { useShellStore } from "./shell-store";
import { signOut } from "./sign-out";
import { useShellData } from "./use-shell-data";

export function UserMenu() {
  const router = useRouter();
  const { resolvedTheme, setTheme } = useTheme();
  const setShortcutsOpen = useShellStore((state) => state.setShortcutsOpen);
  const { me } = useShellData();
  const user = me.data;
  const label = user?.name ?? user?.login ?? "Account";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="flex h-control-lg w-full min-w-0 items-center gap-2 rounded-sm px-2 text-left text-sm hover:bg-surface-3 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none sidebar-collapsed:justify-center sidebar-collapsed:px-0">
        <Avatar className="size-6" aria-hidden>
          {user?.avatar_url && <AvatarImage src={user.avatar_url} alt="" />}
          <AvatarFallback className="text-xs">{label.charAt(0).toUpperCase()}</AvatarFallback>
        </Avatar>
        <span className="flex-1 truncate sidebar-collapsed:sr-only">
          {label}
          <span className="sr-only">(account menu)</span>
        </span>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-60" align="start" side="top">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="truncate">{user?.email ?? user?.login}</DropdownMenuLabel>
          <DropdownMenuItem
            onClick={() => {
              router.push("/settings/profile");
            }}
          >
            <UserRound aria-hidden className="size-4" />
            Profile
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => {
              setTheme(resolvedTheme === "light" ? "dark" : "light");
            }}
          >
            {resolvedTheme === "light" ? (
              <Moon aria-hidden className="size-4" />
            ) : (
              <Sun aria-hidden className="size-4" />
            )}
            {resolvedTheme === "light" ? "Dark theme" : "Light theme"}
          </DropdownMenuItem>
          <DropdownMenuItem
            onClick={() => {
              setShortcutsOpen(true);
            }}
          >
            <Keyboard aria-hidden className="size-4" />
            Keyboard shortcuts
            <DropdownMenuShortcut>
              <KeyHint shortcut={shortcut("shortcuts").keys} />
            </DropdownMenuShortcut>
          </DropdownMenuItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem variant="destructive" onClick={() => void signOut()}>
          <LogOut aria-hidden className="size-4" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
