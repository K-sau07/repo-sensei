import { Skeleton } from "@/components/ui/skeleton";

/*
 * Stand-ins shown while the org-dependent parts of the shell stream in. Each has exactly the
 * geometry of the part it replaces (same width variable, heights and borders), so the swap shifts
 * nothing.
 */

export function SidebarSkeleton() {
  return (
    <div
      aria-hidden
      className="hidden w-(--sidebar-width) shrink-0 flex-col gap-2 border-r border-line bg-surface-1 p-2 in-data-[sidebar=collapsed]:w-14 lg:flex"
    >
      <div className="h-10" />
      <Skeleton className="h-control-lg" />
      <Skeleton className="h-control-md" />
    </div>
  );
}

export function TopBarSkeleton() {
  return <div aria-hidden className="h-12 border-b border-line bg-surface-1 lg:hidden" />;
}

export function BottomBarSkeleton() {
  return <div aria-hidden className="h-14 border-t border-line bg-surface-1 lg:hidden" />;
}
