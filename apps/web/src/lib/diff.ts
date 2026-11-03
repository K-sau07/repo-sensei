/**
 * A unified (git) diff, read for showing (8.12): each file's path and status, and its hunks'
 * lines (added, removed or context) with their old and new line numbers.
 */
export type DiffLineKind = "add" | "del" | "ctx";

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
  old: number | null;
  new: number | null;
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

export interface DiffFile {
  path: string;
  status: "added" | "modified" | "deleted";
  hunks: DiffHunk[];
  added: number;
  removed: number;
}

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/;

export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const path = line.slice("diff --git ".length).split(" b/")[1] ?? line;
      file = { path, status: "modified", hunks: [], added: 0, removed: 0 };
      files.push(file);
      hunk = null;
      continue;
    }
    if (!file) continue;
    const head = HUNK.exec(line);
    if (head) {
      oldNo = Number(head[1]);
      newNo = Number(head[2]);
      hunk = { header: line, lines: [] };
      file.hunks.push(hunk);
    } else if (hunk && line.startsWith("+")) {
      hunk.lines.push({ kind: "add", text: line.slice(1), old: null, new: newNo++ });
      file.added++;
    } else if (hunk && line.startsWith("-")) {
      hunk.lines.push({ kind: "del", text: line.slice(1), old: oldNo++, new: null });
      file.removed++;
    } else if (hunk && line.startsWith(" ")) {
      hunk.lines.push({ kind: "ctx", text: line.slice(1), old: oldNo++, new: newNo++ });
    } else if (!hunk) {
      if (line.startsWith("new file mode")) file.status = "added";
      else if (line.startsWith("deleted file mode")) file.status = "deleted";
    }
  }
  return files;
}
