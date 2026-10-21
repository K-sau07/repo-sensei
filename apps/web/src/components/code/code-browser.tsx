"use client";

import { FileQuestion, Hourglass } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { LockedState } from "@/components/feedback/locked-state";
import { useRepoFeatures } from "@/components/repo/use-repo";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/errors";
import { parentOf } from "@/lib/code/tree";
import { codePath, repoPath } from "@/lib/routes";

import { DirectoryView } from "./directory-view";
import { FileTree } from "./file-tree";
import { FileView } from "./file-view";
import { useCodeLocation, useListing } from "./use-code";

/** Nothing to browse yet: the first index is still running, or never ran. */
function NotIndexed() {
  const { org, repo } = useCodeLocation();
  const { active } = useRepoFeatures();
  return (
    <div className="mx-auto w-full max-w-3xl p-8">
      {active ? (
        // Files can be read once the run has finished (the first index has no earlier one).
        <LockedState
          feature="Code"
          stage="the index run"
          {...(active.eta_s !== null ? { etaSeconds: active.eta_s } : {})}
          href={repoPath(org, repo)}
        />
      ) : (
        <EmptyState
          icon={<Hourglass />}
          title="Not indexed yet"
          description="The code browser opens once this repository has been indexed."
          action={
            <Link
              href={repoPath(org, repo, "index")}
              className="text-sm underline underline-offset-4"
            >
              Indexing
            </Link>
          }
        />
      )}
    </div>
  );
}

/**
 * `/[org]/[repo]/code/…`: the file tree beside whatever is open. It's the code tab's layout, so
 * the tree (what's expanded, where it's scrolled) stays as files open.
 */
export function CodeBrowser({ children }: { children: ReactNode }) {
  const { org, repo, path, ref } = useCodeLocation();
  const router = useRouter();
  const root = useListing("");
  const error = root.error instanceof ApiError ? root.error : null;

  let content: ReactNode;
  if (error?.code === "repo_not_indexed") {
    content = <NotIndexed />;
  } else if (error?.code === "ref_not_indexed" || error?.code === "invalid_ref") {
    content = (
      <div className="mx-auto w-full max-w-3xl p-8">
        <EmptyState
          icon={<FileQuestion />}
          title="Nothing indexed at that commit"
          description={error.message}
          action={
            <Link href={codePath(org, repo, path)} className="text-sm underline underline-offset-4">
              Browse the latest index
            </Link>
          }
        />
      </div>
    );
  } else if (error) {
    content = (
      <div className="mx-auto w-full max-w-3xl p-8">
        <ErrorState
          title="Couldn't load the code"
          description={error.message}
          onRetry={() => void root.refetch()}
          {...(error.requestId ? { requestId: error.requestId } : {})}
        />
      </div>
    );
  }
  if (content) return content;

  return (
    // Fills what the repository header leaves; the tree and the file scroll on their own.
    <div className="relative min-h-[28rem] flex-1">
      <ResizablePanelGroup orientation="horizontal" className="absolute inset-0">
        <ResizablePanel defaultSize="22rem" minSize="14rem" maxSize="50%">
          <FileTree
            className="h-full border-r border-line bg-surface-1"
            current={path}
            onOpenFile={(file) => {
              router.push(codePath(org, repo, file, { ref }));
            }}
          />
        </ResizablePanel>
        <ResizableHandle />
        <ResizablePanel minSize="30%">
          <div className="flex h-full min-w-0 flex-col">{children}</div>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  );
}

/** The page under the tree: the root and directories are listed, files open in the viewer.
 * Which one a path is comes from its parent's listing (the tree has it already). */
export function CodePathView() {
  const { org, repo, path, ref } = useCodeLocation();
  const parent = useListing(parentOf(path), path !== "");
  if (path === "") return <DirectoryView path="" />;
  if (parent.data) {
    const name = path.slice(path.lastIndexOf("/") + 1);
    const entry = parent.data.entries.find((item) => item.name === name);
    if (entry?.type === "dir") return <DirectoryView path={path} />;
    if (entry) return <FileView path={path} />;
  }
  if (parent.data || (parent.error instanceof ApiError && parent.error.status === 404)) {
    return (
      <div className="mx-auto w-full max-w-xl p-8">
        <EmptyState
          icon={<FileQuestion />}
          title="Nothing at this path"
          description={`${path} isn't in this snapshot of the repository.`}
          action={
            <Link
              href={codePath(org, repo, "", { ref })}
              className="text-sm underline underline-offset-4"
            >
              All files
            </Link>
          }
        />
      </div>
    );
  }
  if (parent.isError) {
    return (
      <div className="p-6">
        <ErrorState
          title="Couldn't open this path"
          description={parent.error.message}
          onRetry={() => void parent.refetch()}
        />
      </div>
    );
  }
  return (
    <div role="status" aria-label="Loading" className="flex flex-col gap-2 p-4">
      <Skeleton className="h-6 w-64" />
      <Skeleton className="h-64" />
    </div>
  );
}
