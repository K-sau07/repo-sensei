"use client";

import { File as FileIcon, Folder } from "lucide-react";
import Link from "next/link";

import { ErrorState } from "@/components/feedback/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatLineCount, languageName } from "@/lib/code/format";
import { formatBytes } from "@/lib/constellation";
import { codePath } from "@/lib/routes";

import { PathHeader } from "./path-header";
import { useCodeLocation, useListing } from "./use-code";

const count = (n: number, noun: string) => `${n.toLocaleString("en")} ${noun}${n === 1 ? "" : "s"}`;

// A table this long is still quick to render; past it, the (virtualized) tree takes over.
const MAX_ROWS = 1000;

/** A directory in the main pane: what it holds, each entry a link. */
export function DirectoryView({ path }: { path: string }) {
  const { org, repo, ref } = useCodeLocation();
  const listing = useListing(path);
  const entries = listing.data?.entries ?? [];
  const files = entries.filter((entry) => entry.type === "file");
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PathHeader
        path={path}
        meta={
          listing.data && (
            <span className="tabular-nums">
              {count(entries.length - files.length, "folder")} · {count(files.length, "file")}
            </span>
          )
        }
      />
      <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
        {listing.data ? (
          <Table aria-label={`Contents of ${path || repo}`}>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Language</TableHead>
                <TableHead className="text-right">Lines</TableHead>
                <TableHead className="text-right">Size</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.slice(0, MAX_ROWS).map((entry) => (
                <TableRow key={entry.path}>
                  <TableCell>
                    <Link
                      href={codePath(org, repo, entry.path, { ref })}
                      className="inline-flex items-center gap-2 font-mono underline-offset-4 hover:underline"
                    >
                      {entry.type === "dir" ? (
                        <Folder aria-hidden className="size-4 text-fg-faint" />
                      ) : (
                        <FileIcon aria-hidden className="size-4 text-fg-faint" />
                      )}
                      {entry.name}
                      {entry.type === "dir" && <span className="sr-only">(folder)</span>}
                    </Link>
                  </TableCell>
                  <TableCell className="text-fg-muted">
                    {entry.type === "dir"
                      ? `${String(entry.child_count ?? 0)} ${entry.child_count === 1 ? "item" : "items"}`
                      : (languageName(entry.language) ?? "–")}
                  </TableCell>
                  <TableCell className="text-right text-fg-muted tabular-nums">
                    {typeof entry.loc === "number" ? formatLineCount(entry.loc) : "–"}
                  </TableCell>
                  <TableCell className="text-right text-fg-muted tabular-nums">
                    {typeof entry.size === "number" ? formatBytes(entry.size) : "–"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
            {entries.length > MAX_ROWS && (
              <caption className="caption-bottom pt-3 text-left text-sm text-fg-muted">
                The first {MAX_ROWS.toLocaleString("en")} of {entries.length.toLocaleString("en")}{" "}
                entries; the file tree has them all.
              </caption>
            )}
          </Table>
        ) : listing.isError ? (
          <ErrorState
            title="Couldn't load this folder"
            description={listing.error.message}
            onRetry={() => void listing.refetch()}
          />
        ) : (
          <div role="status" aria-label="Loading the folder" className="flex flex-col gap-2">
            {Array.from({ length: 6 }, (_, index) => (
              <Skeleton key={index} className="h-6" />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
