"use client";

import { FileQuestion, FolderTree, Hourglass } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { LockedState } from "@/components/feedback/locked-state";
import { useRepoFeatures } from "@/components/repo/use-repo";
import { useRightRail } from "@/components/shell/right-rail";
import { Button } from "@/components/ui/button";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { LG, useMediaQuery } from "@/hooks/use-media-query";
import { ApiError } from "@/lib/api/errors";
import { parentOf } from "@/lib/code/tree";
import { STAGE_LABELS } from "@/lib/indexing";
import { codePath, repoPath } from "@/lib/routes";

import { DirectoryView } from "./directory-view";
import { FileTree } from "./file-tree";
import { FileView } from "./file-view";
import { ReferencesDrawer } from "./references-drawer";
import { SymbolRail } from "./symbol-rail";
import { useCodeLocation, useListing } from "./use-code";

/** Nothing to browse yet: the first index hasn't got to its files, or never ran. */
function NotIndexed() {
  const { org, repo } = useCodeLocation();
  const { active, state } = useRepoFeatures();
  // Files can be read once discovery is done (the first run's snapshot answers from then on).
  const code = state("code_browser");
  return (
    <div className="mx-auto w-full max-w-3xl p-8">
      {active ? (
        <LockedState
          feature="Code"
          stage={code.stage ? STAGE_LABELS[code.stage] : "indexing"}
          {...(code.etaSeconds !== null ? { etaSeconds: code.etaSeconds } : {})}
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
 * the tree (what's expanded, where it's scrolled) stays as files open. Below `lg` (tablets and
 * phones) the file has the width and the tree is a drawer, closed again once a file opens.
 */
export function CodeBrowser({ children }: { children: ReactNode }) {
  const { org, repo, path, ref } = useCodeLocation();
  const router = useRouter();
  const wide = useMediaQuery(LG);
  const [filesOpen, setFilesOpen] = useState(false);
  // The drawer is for narrow windows: one left open doesn't come back when the window narrows
  // again after widening.
  if (wide && filesOpen) setFilesOpen(false);
  const drawerOpen = filesOpen && !wide;
  const root = useListing("");
  const error = root.error instanceof ApiError ? root.error : null;
  // The rail reads where it is itself, so its element never changes.
  useRightRail(
    "Symbol",
    useMemo(() => <SymbolRail />, []),
  );

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

  // One tree for every width, so the open file stays mounted (its scroll, its selection) as the
  // window crosses `lg`: the tree's panel is only left out below it (a hole in the children, so
  // the file's panel keeps its place), and the drawer offered instead.
  return (
    <div className="flex min-h-[28rem] flex-1 flex-col">
      {!wide && (
        <div className="flex items-center border-b border-line px-2 py-1">
          <Sheet open={drawerOpen} onOpenChange={setFilesOpen}>
            <SheetTrigger render={<Button variant="ghost" size="sm" />}>
              <FolderTree aria-hidden />
              Files
            </SheetTrigger>
            <SheetContent side="left" className="w-80 max-w-[85vw] gap-0 p-0">
              <SheetTitle className="sr-only">Files</SheetTitle>
              <FileTree
                className="h-full bg-surface-1"
                current={path}
                onOpenFile={(file) => {
                  setFilesOpen(false);
                  router.push(codePath(org, repo, file, { ref }));
                }}
              />
            </SheetContent>
          </Sheet>
        </div>
      )}
      {/* Fills what the repository header leaves; the tree and the file scroll on their own. */}
      <div className="relative min-h-0 flex-1">
        <ResizablePanelGroup orientation="horizontal" className="absolute inset-0">
          {wide && (
            <ResizablePanel id="files" defaultSize="22rem" minSize="14rem" maxSize="50%">
              <FileTree
                className="h-full border-r border-line bg-surface-1"
                current={path}
                onOpenFile={(file) => {
                  router.push(codePath(org, repo, file, { ref }));
                }}
              />
            </ResizablePanel>
          )}
          {wide && <ResizableHandle />}
          <ResizablePanel id="file" minSize="30%">
            <div className="flex h-full min-w-0 flex-col">
              {children}
              <ReferencesDrawer />
            </div>
          </ResizablePanel>
        </ResizablePanelGroup>
      </div>
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
