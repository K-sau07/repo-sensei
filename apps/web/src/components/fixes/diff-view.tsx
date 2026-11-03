/**
 * A patch as a reviewer reads it (8.12): the code it changes first, then the tests it adds, each
 * file with its lines added and removed, numbered as before and after.
 */
import { cn } from "cn";

import { parseDiff, type DiffFile } from "@/lib/diff";

const TEST = /(^|\/)(tests?|__tests__)\/|(^|\/)test_[^/]+\.py$|\.(test|spec)\.[jt]sx?$/;

/** Code first (what the change is), its tests after (how it is shown to work). */
export function readingOrder(files: DiffFile[]): DiffFile[] {
  return [...files.filter((f) => !TEST.test(f.path)), ...files.filter((f) => TEST.test(f.path))];
}

export function DiffView({ diff }: { diff: string }) {
  const files = readingOrder(parseDiff(diff));
  if (files.length === 0) return <p className="text-xs text-fg-muted">No changes.</p>;
  return (
    <div className="flex flex-col gap-3" data-testid="diff">
      {files.map((file) => (
        <section
          key={file.path}
          aria-label={`Changes to ${file.path}`}
          className="overflow-hidden rounded-md border border-line"
          data-testid="diff-file"
        >
          <header className="flex items-center gap-2 border-b border-line bg-surface-2 px-3 py-1.5 text-xs">
            <span className="font-mono">{file.path}</span>
            {file.status !== "modified" && <span className="text-signal">{file.status}</span>}
            <span className="ml-auto tabular-nums">
              <span className="text-ok">+{file.added}</span>{" "}
              <span className="text-danger">−{file.removed}</span>
              <span className="sr-only">
                {" "}
                ({file.added} lines added, {file.removed} removed)
              </span>
            </span>
          </header>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse font-mono text-xs">
              <tbody>
                {file.hunks.flatMap((hunk, h) => [
                  <tr key={`h${String(h)}`} className="bg-surface-2/60 text-fg-muted">
                    <td colSpan={3} className="px-3 py-0.5">
                      {hunk.header}
                    </td>
                  </tr>,
                  ...hunk.lines.map((line, i) => (
                    <tr
                      key={`${String(h)}:${String(i)}`}
                      data-kind={line.kind}
                      className={cn(
                        line.kind === "add" && "bg-ok/10",
                        line.kind === "del" && "bg-danger/10",
                      )}
                    >
                      <td className="w-10 px-2 text-right text-fg-muted select-none">
                        {line.old ?? ""}
                      </td>
                      <td className="w-10 px-2 text-right text-fg-muted select-none">
                        {line.new ?? ""}
                      </td>
                      <td className="px-2 whitespace-pre">
                        <span aria-hidden className="select-none">
                          {line.kind === "add" ? "+" : line.kind === "del" ? "−" : " "}
                        </span>
                        <span className="sr-only">
                          {line.kind === "add" ? "added: " : line.kind === "del" ? "removed: " : ""}
                        </span>
                        {line.text}
                      </td>
                    </tr>
                  )),
                ])}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  );
}
