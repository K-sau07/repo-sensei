"use client";

/**
 * A diagram's node, opened beside the canvas (6.6): its code at the diagram's commit, a question
 * about it (the Ask tab, scoped to its lines), its trace, and "Expand this step": a diagram of
 * the flow from it, drawn from the graph. A part of the whole app's architecture: the folders it is
 * made of (each opening in the code browser) and a question about it (scoped to its folder); a
 * system outside the app or its users: what they are. A table of an ER diagram: its model's code.
 */
import { CornerDownRight, FolderOpen, Route as RouteIcon, X } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { askHref } from "@/components/ask/ask-bar";
import { CodeViewer } from "@/components/code/code-viewer";
import { useFileAt } from "@/components/code/use-code";
import { useRepoSlug } from "@/components/repo/use-repo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { citationLabel, type SpecNode } from "@/lib/api/diagrams";
import { repoPath } from "@/lib/routes";
import { traceHref } from "@/lib/trace/url-state";

import { laneOf } from "./blueprint-node";
import { kindWords } from "./system-node";

export function NodePanel({
  node,
  sha,
  onClose,
  onExpand,
  expanding,
}: {
  node: SpecNode;
  sha: string;
  onClose: () => void;
  onExpand?: ((node: SpecNode) => void) | undefined;
  expanding?: boolean;
}) {
  const { org, repo } = useRepoSlug();
  const router = useRouter();
  const [question, setQuestion] = useState("");
  const cited = node.citation;
  const file = useFileAt(cited?.path ?? "", sha, cited !== null);
  const content = cited && file.data && "content" in file.data ? file.data.content : null;
  const language = file.data && "language" in file.data ? (file.data.language ?? null) : null;
  const lane = laneOf(node.layer);
  const isModule = node.kind === "module"; // a module's id is its folder
  const part = node.id.startsWith("part:"); // an architecture's part: its members are folders
  const outside = node.layer === "outside" || node.layer === "actor"; // not the repository's
  const folders = isModule || part;
  const folder = isModule ? node.id : part ? (node.members?.[0]?.name ?? null) : null;
  const ask = () => {
    const text =
      question.trim() ||
      (node.layer === "outside"
        ? `How does the app use ${node.label}?`
        : `How does ${node.label} work?`);
    router.push(
      askHref(org, repo, text, {
        path: folders && (node.members?.length ?? 0) <= 1 ? folder : (cited?.path ?? null),
        startLine: folders ? null : (cited?.start_line ?? null),
        endLine: folders ? null : (cited?.end_line ?? null),
      }),
    );
  };
  return (
    <aside
      aria-label={node.label}
      className="flex h-full min-h-0 flex-col border-l border-line"
      data-testid="node-panel"
    >
      <header className="flex items-start gap-2 border-b border-line px-3 py-2">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className={`text-[10px] tracking-wide uppercase ${lane.text}`}>
            <span aria-hidden>{lane.glyph} </span>
            {lane.label}
          </span>
          <h2 className="truncate text-sm font-semibold">{node.label}</h2>
          <span className="truncate font-mono text-[11px] text-fg-muted" title={node.id}>
            {citationLabel(cited) ??
              (part
                ? `${node.layer} · ${String(node.members?.length ?? 0)} folders`
                : kindWords(node.kind))}
          </span>
        </div>
        <Button size="icon-sm" variant="ghost" aria-label="Close" onClick={onClose}>
          <X aria-hidden />
        </Button>
      </header>
      <div className="flex flex-wrap gap-1 border-b border-line px-3 py-2">
        {isModule ? (
          <Link
            href={`${repoPath(org, repo, `code/${node.id}`)}?ref=${sha}` as Route}
            className="inline-flex h-7 items-center gap-1.5 rounded-sm px-2 text-xs text-fg-muted hover:bg-surface-2 hover:text-fg"
          >
            <FolderOpen aria-hidden className="size-3.5" />
            Open the folder
          </Link>
        ) : null}
        {folders || outside ? null : (
          <Link
            href={`${traceHref(org, repo, { from: node.id })}&ref=${sha}` as Route}
            className="inline-flex h-7 items-center gap-1.5 rounded-sm px-2 text-xs text-fg-muted hover:bg-surface-2 hover:text-fg"
          >
            <RouteIcon aria-hidden className="size-3.5" />
            Trace from here
          </Link>
        )}
        {onExpand && !folders && !outside ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={expanding}
            onClick={() => {
              onExpand(node);
            }}
          >
            <CornerDownRight aria-hidden />
            {expanding ? "Drawing…" : "Expand this step"}
          </Button>
        ) : null}
      </div>
      <form
        className="flex gap-1.5 border-b border-line px-3 py-2"
        onSubmit={(event) => {
          event.preventDefault();
          ask();
        }}
      >
        <Input
          aria-label={`Ask about ${node.label}`}
          placeholder={`Ask about ${node.label}…`}
          value={question}
          onChange={(event) => {
            setQuestion(event.target.value);
          }}
          className="h-8 text-xs"
        />
        <Button type="submit" size="sm" variant="outline">
          Ask
        </Button>
      </form>
      <div className="min-h-0 flex-1 overflow-auto">
        {folders ? (
          <ModuleParts node={node} sha={sha} />
        ) : outside ? (
          <p className="p-3 text-xs text-fg-muted">
            {node.layer === "actor"
              ? "Who uses the app: not in its code. The line to it is inferred, and says why."
              : `Outside the repository: the code reaches ${node.label} through the packages it imports${
                  node.kind === "database" ? ", and reads and writes its tables there" : ""
                }. Each line to it is the graph's.`}
          </p>
        ) : cited && content !== null ? (
          <CodeViewer
            path={cited.path}
            language={language}
            content={content}
            lines={{ start: cited.start_line, end: cited.end_line }}
            onLinesChange={() => undefined}
            wrap={false}
          />
        ) : (
          <p className="p-3 text-xs text-fg-faint">
            {!cited
              ? "This step has no file of its own (an endpoint or a table)."
              : file.isPending
                ? "Loading…"
                : "This file can't be shown here."}
          </p>
        )}
      </div>
      <p className="border-t border-line px-3 py-1.5 text-[11px] text-fg-faint">
        At {sha.slice(0, 7)} ·{" "}
        <Link href={repoPath(org, repo, `code/${cited?.path ?? ""}`)} className="hover:underline">
          open in the code browser
        </Link>
      </p>
    </aside>
  );
}

/** What a module holds, or the folders a part is made of (each opening in the code browser). */
function ModuleParts({ node, sha }: { node: SpecNode; sha: string }) {
  const { org, repo } = useRepoSlug();
  const members = node.members ?? [];
  const more = node.more_members ?? 0;
  if (members.length > 0 && members.every((member) => member.kind === "module")) {
    return (
      <div className="flex flex-col gap-2 p-3">
        {node.note ? <p className="text-xs text-fg-muted">{node.note}</p> : null}
        <ul className="flex flex-col gap-1" aria-label={`The folders ${node.label} is made of`}>
          {members.map((member) => (
            <li key={member.name}>
              <Link
                href={`${repoPath(org, repo, `code/${member.name}`)}?ref=${sha}` as Route}
                className="inline-flex items-center gap-1.5 font-mono text-xs text-fg hover:underline"
              >
                <FolderOpen aria-hidden className="size-3.5 text-fg-faint" />
                {member.name}
              </Link>
            </li>
          ))}
          {more > 0 ? <li className="text-xs text-fg-faint">and {more} more</li> : null}
        </ul>
      </div>
    );
  }
  if (members.length === 0) {
    return (
      <p className="p-3 text-xs text-fg-faint">
        No endpoints, tables or components in it: its code is functions and classes. Open the folder
        to read it.
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-1 p-3" aria-label={`What ${node.label} holds`}>
      {members.map((member) => (
        <li key={`${member.kind}:${member.name}`} className="flex items-baseline gap-2 text-xs">
          <span className="w-20 shrink-0 text-fg-faint">{kindWords(member.kind)}</span>
          <span className="truncate font-mono text-fg">{member.name}</span>
        </li>
      ))}
      {more > 0 ? <li className="text-xs text-fg-faint">and {more} more</li> : null}
    </ul>
  );
}
