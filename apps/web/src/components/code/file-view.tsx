"use client";

import {
  Download,
  ExternalLink,
  FileX2,
  Link as LinkIcon,
  Search,
  TextSelect,
  WrapText,
  X,
} from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useRef, useState } from "react";

import { EmptyState } from "@/components/feedback/empty-state";
import { ErrorState } from "@/components/feedback/error-state";
import { CopyButton } from "@/components/primitives/copy-button";
import { KeyHint } from "@/components/primitives/key-hint";
import { useRepo } from "@/components/repo/use-repo";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Toggle } from "@/components/ui/toggle";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useShortcut } from "@/hooks/use-shortcut";
import { ApiError } from "@/lib/api/errors";
import { formatLineCount, languageName } from "@/lib/code/format";
import { describeLines, formatLines, sameLines, type LineRange } from "@/lib/code/lines";
import { formatBytes } from "@/lib/constellation";
import { codePath } from "@/lib/routes";
import { shortcut } from "@/lib/shortcut-registry";

import type { CodeViewerHandle } from "./code-viewer";
import { PathHeader } from "./path-header";
import { useCodeLocation, useFile } from "./use-code";

// CodeMirror runs in the browser only, in its own chunk.
const CodeViewer = dynamic(() => import("./code-viewer").then((module) => module.CodeViewer), {
  ssr: false,
  loading: () => <ViewerSkeleton />,
});

function ViewerSkeleton() {
  return (
    <div role="status" aria-label="Loading the file" className="flex flex-col gap-2 p-4">
      {Array.from({ length: 12 }, (_, index) => (
        <Skeleton
          key={index}
          className="h-3.5"
          style={{ width: `${String(30 + ((index * 29) % 60))}%` }}
        />
      ))}
    </div>
  );
}

/** `lines` cut to the file's length: none if they start past its end. */
export function withinFile(lines: LineRange | null, content: string): LineRange | null {
  if (!lines) return null;
  const count = content.split("\n").length;
  if (lines.start > count) return null;
  return lines.end > count ? { start: lines.start, end: count } : lines;
}

/** The metadata a 413 or 415 carries (`details`), when it has the expected shape. */
export interface FileDetails {
  path: string;
  language: string | null;
  size: number;
  loc: number | null;
  commit_sha: string | null;
  download_url: string | null;
}

export function fileDetails(details: unknown): FileDetails | null {
  if (typeof details !== "object" || details === null) return null;
  const record: Record<string, unknown> = { ...details };
  if (typeof record.path !== "string" || typeof record.size !== "number") return null;
  const text = (value: unknown) => (typeof value === "string" ? value : null);
  return {
    path: record.path,
    language: text(record.language),
    size: record.size,
    loc: typeof record.loc === "number" ? record.loc : null,
    commit_sha: text(record.commit_sha),
    download_url: text(record.download_url),
  };
}

/** GitHub's page for the file at `commit` (the default branch if unknown), lines included. */
export function githubBlobUrl(
  fullName: string,
  commit: string,
  path: string,
  lines: LineRange | null = null,
): string {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  const at = encodeRef(commit);
  const anchor = lines
    ? lines.start === lines.end
      ? `#L${String(lines.start)}`
      : `#L${String(lines.start)}-L${String(lines.end)}`
    : "";
  return `https://github.com/${fullName}/blob/${at}/${encoded}${anchor}`;
}

/** A commit or ref as a URL path: each segment encoded ("feat#2" must not end the URL), the
 * slashes of a branch name kept. */
function encodeRef(ref: string): string {
  return ref.split("/").map(encodeURIComponent).join("/");
}

function githubRawUrl(fullName: string, commit: string, path: string): string {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return `https://github.com/${fullName}/raw/${encodeRef(commit)}/${encoded}`;
}

function GitHubLink({ href, label = "Open on GitHub" }: { href: string; label?: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className={buttonVariants({ variant: "ghost", size: "sm" })}
    >
      <ExternalLink aria-hidden data-icon="inline-start" />
      {label}
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  );
}

/** A file that can't be shown: binary (415) or over 2 MB (413), with a way to get it: the API's
 * download link (GitHub's raw file only if there is none). */
function UnviewableFile({
  kind,
  details,
  githubUrl,
  rawUrl,
}: {
  kind: "binary" | "large";
  details: FileDetails | null;
  githubUrl: string | null;
  rawUrl: string | null;
}) {
  const ours = details?.download_url ?? null;
  const download = ours ?? (kind === "binary" ? rawUrl : null);
  return (
    <div className="mx-auto w-full max-w-xl p-8">
      <EmptyState
        icon={<FileX2 />}
        title={kind === "binary" ? "This file isn't text" : "This file is too large to show"}
        description={
          <>
            {details ? formatBytes(details.size) : null}
            {details?.language ? ` · ${languageName(details.language) ?? ""}` : null}
            {kind === "binary"
              ? ". Binary files (images, archives, fonts) aren't shown here; download it instead."
              : ". Files over 2 MB aren't shown here; download it instead."}
          </>
        }
        action={
          <div className="flex flex-wrap items-center justify-center gap-2">
            {download && (
              <a
                href={download}
                {...(ours ? { download: "" } : { target: "_blank", rel: "noreferrer" })}
                className={buttonVariants({ variant: "outline", size: "sm" })}
              >
                <Download aria-hidden data-icon="inline-start" />
                {ours ? "Download" : "Download from GitHub"}
              </a>
            )}
            {githubUrl && <GitHubLink href={githubUrl} />}
          </div>
        }
      />
    </div>
  );
}

/** A file in the main pane: its path and facts, then the read-only viewer (or why it can't be
 * shown). Lines picked are in `?L=`, so a link brings the reader back to them. */
export function FileView({ path }: { path: string }) {
  const { org, repo, ref, lines, setLines } = useCodeLocation();
  const repoInfo = useRepo();
  const file = useFile(path);
  const [wrap, setWrap] = useState(false);
  const [selection, setSelection] = useState<LineRange | null>(null);
  const viewer = useRef<CodeViewerHandle>(null);
  const error = file.error instanceof ApiError ? file.error : null;
  const details = fileDetails(error?.details);
  const commit = file.data?.commit ?? details?.commit_sha ?? ref ?? null;
  const fullName = repoInfo.data?.full_name ?? null;
  const branch = repoInfo.data?.default_branch ?? "HEAD";
  // The lines in the URL, if the file has them (a `?L=` past its end picks nothing).
  const shown = file.data ? withinFile(lines, file.data.content) : lines;
  const githubUrl = fullName ? githubBlobUrl(fullName, commit ?? branch, path, shown) : null;

  useShortcut(
    "code-search",
    () => {
      viewer.current?.openSearch();
    },
    // From the page (also while focus is in the tree's filter), not inside a dialog.
    { enabled: file.data !== undefined, inInputs: true, inOverlays: false },
  );

  const permalink =
    typeof window === "undefined"
      ? ""
      : `${window.location.origin}${codePath(org, repo, path, {
          ref: commit,
          lines: shown ? formatLines(shown) : null,
        })}`;

  let body;
  if (file.data) {
    body = (
      <CodeViewer
        path={path}
        language={file.data.language}
        content={file.data.content}
        lines={lines}
        onLinesChange={(next) => void setLines(next)}
        onSelectionLines={setSelection}
        wrap={wrap}
        handle={viewer}
      />
    );
  } else if (error?.code === "binary_file" || error?.code === "file_too_large") {
    body = (
      <UnviewableFile
        kind={error.code === "binary_file" ? "binary" : "large"}
        details={details}
        githubUrl={fullName ? githubBlobUrl(fullName, commit ?? branch, path) : null}
        rawUrl={fullName ? githubRawUrl(fullName, commit ?? branch, path) : null}
      />
    );
  } else if (error?.status === 404) {
    body = (
      <div className="mx-auto w-full max-w-xl p-8">
        <EmptyState
          icon={<FileX2 />}
          title="No such file"
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
  } else if (file.isError) {
    body = (
      <div className="p-6">
        <ErrorState
          title="Couldn't open this file"
          description={file.error.message}
          onRetry={() => void file.refetch()}
          {...(error?.requestId ? { requestId: error.requestId } : {})}
        />
      </div>
    );
  } else {
    body = <ViewerSkeleton />;
  }

  const data = file.data;
  const facts = data ?? details;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PathHeader
        path={path}
        meta={
          facts && (
            <>
              {languageName(facts.language) && <span>{languageName(facts.language)}</span>}
              {facts.loc !== null && (
                <span className="tabular-nums">{formatLineCount(facts.loc)}</span>
              )}
              <span className="tabular-nums">{formatBytes(facts.size)}</span>
              {data?.flags.includes("generated") && <Badge variant="outline">Generated</Badge>}
              {commit && (
                <span className="font-mono" title={commit}>
                  {ref ? "Pinned to " : "At "}
                  {commit.slice(0, 7)}
                </span>
              )}
              {ref && (
                <Link
                  href={codePath(org, repo, path, { lines: lines ? formatLines(lines) : null })}
                  className="underline underline-offset-4"
                >
                  View the latest
                </Link>
              )}
            </>
          )
        }
        actions={
          <>
            {shown && data && (
              <span className="inline-flex items-center gap-0.5 rounded-sm bg-warn/12 py-0.5 pr-0.5 pl-2 text-xs text-fg">
                {describeLines(shown)}
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Clear the selected lines"
                  onClick={() => void setLines(null)}
                >
                  <X aria-hidden />
                </Button>
              </span>
            )}
            {selection && data && !sameLines(selection, shown) && (
              <Button variant="ghost" size="sm" onClick={() => void setLines(selection)}>
                <TextSelect aria-hidden data-icon="inline-start" />
                Link {describeLines(selection).toLowerCase()}
              </Button>
            )}
            {data && (
              <>
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label="Search in the file"
                        onClick={() => {
                          viewer.current?.openSearch();
                        }}
                      />
                    }
                  >
                    <Search aria-hidden />
                  </TooltipTrigger>
                  <TooltipContent>
                    Search in the file <KeyHint shortcut={shortcut("code-search").keys} />
                  </TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <Toggle
                        size="sm"
                        aria-label="Wrap long lines"
                        pressed={wrap}
                        onPressedChange={setWrap}
                      />
                    }
                  >
                    <WrapText aria-hidden />
                  </TooltipTrigger>
                  <TooltipContent>Wrap long lines</TooltipContent>
                </Tooltip>
                <CopyButton
                  value={permalink}
                  icon={<LinkIcon />}
                  label={
                    shown
                      ? `Copy a permalink to ${describeLines(shown).toLowerCase()}`
                      : "Copy a permalink"
                  }
                />
              </>
            )}
            {githubUrl && <GitHubLink href={githubUrl} />}
          </>
        }
      />
      <div className="relative min-h-0 flex-1">{body}</div>
    </div>
  );
}
