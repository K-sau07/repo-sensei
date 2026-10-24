"use client";

import Link from "next/link";
import { Fragment, type ReactNode } from "react";

import { CopyButton } from "@/components/primitives/copy-button";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { codePath } from "@/lib/routes";

import { useCodeLocation } from "./use-code";

/** `repo / backend / app / main.py`: every directory above is a link; the last is the heading. */
export function PathBreadcrumbs({ path }: { path: string }) {
  const { org, repo, ref } = useCodeLocation();
  const parts = path.split("/").filter(Boolean);
  const crumbs = [
    { label: repo, path: "" },
    ...parts.map((part, index) => ({
      label: part,
      path: parts.slice(0, index + 1).join("/"),
    })),
  ];
  const last = crumbs.length - 1;
  return (
    <Breadcrumb aria-label="Path" className="min-w-0">
      <BreadcrumbList className="gap-1 font-mono text-sm sm:gap-1">
        {crumbs.map((crumb, index) => (
          <Fragment key={crumb.path}>
            {index > 0 && <BreadcrumbSeparator>/</BreadcrumbSeparator>}
            <BreadcrumbItem className="min-w-0">
              {index === last ? (
                <h2 className="truncate font-medium text-fg" aria-current="page">
                  {crumb.label}
                </h2>
              ) : (
                <BreadcrumbLink
                  render={<Link href={codePath(org, repo, crumb.path, { ref })} />}
                  className="underline-offset-4 hover:underline"
                >
                  {crumb.label}
                </BreadcrumbLink>
              )}
            </BreadcrumbItem>
          </Fragment>
        ))}
      </BreadcrumbList>
    </Breadcrumb>
  );
}

/** The bar above a file or directory: where it is, then what it is and what to do with it. */
export function PathHeader({
  path,
  meta,
  actions,
}: {
  path: string;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="flex shrink-0 flex-col gap-1 border-b border-line px-4 py-2">
      <div className="flex min-h-control-sm flex-wrap items-center gap-x-3 gap-y-1">
        <div className="flex min-w-0 items-center gap-0.5">
          <PathBreadcrumbs path={path} />
          {path && <CopyButton value={path} label="Copy the path" />}
        </div>
        {actions && <div className="ml-auto flex items-center gap-1">{actions}</div>}
      </div>
      {meta && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted">
          {meta}
        </div>
      )}
    </header>
  );
}
