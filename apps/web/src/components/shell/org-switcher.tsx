"use client";

import { Check, ChevronsUpDown, Plus } from "lucide-react";
import { useRouter } from "next/navigation";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { orgPath } from "@/lib/routes";

import { useShellData } from "./use-shell-data";

function OrgMark({ name }: { name: string }) {
  return (
    <span
      aria-hidden
      className="flex size-6 shrink-0 items-center justify-center rounded-sm bg-surface-3 text-xs font-semibold text-fg"
    >
      {name.charAt(0).toUpperCase()}
    </span>
  );
}

export function OrgSwitcher() {
  const router = useRouter();
  const { me, slug, currentOrg } = useShellData();

  if (me.isPending) {
    return <Skeleton className="h-control-lg w-full" aria-hidden />;
  }
  const name = currentOrg?.name ?? slug ?? "Choose an organization";
  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="flex h-control-lg w-full min-w-0 items-center gap-2 rounded-sm px-2 text-left text-sm font-medium hover:bg-surface-3 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none in-data-[sidebar=collapsed]:justify-center in-data-[sidebar=collapsed]:px-0">
        <OrgMark name={name} />
        {/* Named by its content, so the visible text is the start of the name (WCAG 2.5.3). */}
        <span className="flex-1 truncate in-data-[sidebar=collapsed]:sr-only">
          {name}
          <span className="sr-only">(switch organization)</span>
        </span>
        <ChevronsUpDown
          aria-hidden
          className="size-3.5 text-fg-faint in-data-[sidebar=collapsed]:hidden"
        />
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-60" align="start">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Organizations</DropdownMenuLabel>
          {(me.data?.orgs ?? []).map((org) => (
            <DropdownMenuItem
              key={org.slug}
              onClick={() => {
                router.push(orgPath(org.slug));
              }}
            >
              <OrgMark name={org.name} />
              <span className="flex-1 truncate">{org.name}</span>
              {org.slug === slug && <Check aria-label="Current" className="size-4" />}
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={() => {
            router.push("/onboarding");
          }}
        >
          <Plus aria-hidden className="size-4" />
          New organization
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
