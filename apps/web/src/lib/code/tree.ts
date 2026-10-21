/**
 * The file tree as a flat list of visible rows: directories are loaded one level at a time
 * (`GET /tree?path=`), and only the expanded ones contribute their children. A flat list is what
 * the virtualizer renders and what keyboard navigation moves through, so a tree of 100k files
 * costs no more than the rows on screen.
 */
import type { Layer } from "@/components/primitives/layer-badge";
import type { TreeEntry } from "@/lib/api/types";

/** A directory's children, as far as they are known. */
export type DirState =
  { status: "loaded"; entries: readonly TreeEntry[] } | { status: "loading" } | { status: "error" };

export interface TreeRow {
  entry: TreeEntry;
  /** 1 for the root's children (as `aria-level`). */
  level: number;
  /** Position among its siblings, from 1, and how many there are (`aria-posinset`/`setsize`). */
  position: number;
  siblings: number;
  /** Only for directories. */
  expanded: boolean;
  /** An expanded directory whose children haven't arrived (or failed to). */
  status: "loaded" | "loading" | "error";
}

/** "backend/app/main.py" → "backend/app"; a top-level entry's parent is the root (""). */
export function parentOf(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash === -1 ? "" : path.slice(0, slash);
}

/** The directories that hold `path`, outermost first: "a/b/c.py" → ["a", "a/b"]. */
export function ancestorsOf(path: string): string[] {
  const parts = path.split("/").filter(Boolean);
  return parts.slice(0, -1).map((_, index) => parts.slice(0, index + 1).join("/"));
}

/** The visible rows, depth first. Iterative, so a deeply nested tree can't overflow the stack. */
export function flattenTree(
  dir: (path: string) => DirState | undefined,
  expanded: ReadonlySet<string>,
): TreeRow[] {
  const rows: TreeRow[] = [];
  const root = dir("");
  if (root?.status !== "loaded") return rows;
  // Each frame: a directory's entries and the next one to emit.
  const stack: { entries: readonly TreeEntry[]; next: number; level: number }[] = [
    { entries: root.entries, next: 0, level: 1 },
  ];
  while (stack.length > 0) {
    const frame = stack[stack.length - 1];
    if (!frame) break;
    const entry = frame.entries[frame.next];
    if (!entry) {
      stack.pop();
      continue;
    }
    frame.next += 1;
    const isOpen = entry.type === "dir" && expanded.has(entry.path);
    const children = isOpen ? dir(entry.path) : undefined;
    rows.push({
      entry,
      level: frame.level,
      position: frame.next,
      siblings: frame.entries.length,
      expanded: isOpen,
      status: isOpen ? (children?.status ?? "loading") : "loaded",
    });
    if (children?.status === "loaded" && children.entries.length > 0) {
      stack.push({ entries: children.entries, next: 0, level: frame.level + 1 });
    }
  }
  return rows;
}

export type TreeAction =
  | { type: "focus"; index: number }
  | { type: "expand"; path: string }
  | { type: "collapse"; path: string }
  | { type: "activate"; index: number };

/**
 * What a key does on row `index`, following the WAI-ARIA tree pattern: ↑↓ move, → opens a
 * directory (or moves into an open one), ← closes it (or moves to the parent), Home/End jump,
 * Enter/Space activate. Type-ahead is separate (`typeAhead`).
 */
export function treeKeyAction(
  key: string,
  rows: readonly TreeRow[],
  index: number,
): TreeAction | null {
  const row = rows[index];
  if (!row) return rows.length > 0 ? { type: "focus", index: 0 } : null;
  const isDir = row.entry.type === "dir";
  switch (key) {
    case "ArrowDown":
      return index + 1 < rows.length ? { type: "focus", index: index + 1 } : null;
    case "ArrowUp":
      return index > 0 ? { type: "focus", index: index - 1 } : null;
    case "Home":
      return { type: "focus", index: 0 };
    case "End":
      return { type: "focus", index: rows.length - 1 };
    case "ArrowRight":
      if (!isDir) return null;
      if (!row.expanded) return { type: "expand", path: row.entry.path };
      return rows[index + 1]?.level === row.level + 1 ? { type: "focus", index: index + 1 } : null;
    case "ArrowLeft": {
      if (isDir && row.expanded) return { type: "collapse", path: row.entry.path };
      const parent = parentOf(row.entry.path);
      if (!parent) return null;
      const at = rows.findLastIndex((other, i) => i < index && other.entry.path === parent);
      return at === -1 ? null : { type: "focus", index: at };
    }
    case "Enter":
    case " ":
      return { type: "activate", index };
    default:
      return null;
  }
}

/**
 * Type-ahead: the next row whose name starts with what was typed, searching down from the
 * current row and wrapping around. Typing one letter repeatedly ("ccc") cycles through the rows
 * starting with it; a longer prefix keeps the current row while it still matches.
 */
export function typeAhead(rows: readonly TreeRow[], from: number, typed: string): number {
  const query = typed.toLowerCase();
  if (!query || rows.length === 0) return -1;
  const cycling = /^(.)\1*$/su.test(query);
  const needle = cycling ? query.charAt(0) : query;
  const offset = cycling ? 1 : 0;
  for (let step = 0; step < rows.length; step += 1) {
    const index = (Math.max(from, 0) + offset + step) % rows.length;
    if (rows[index]?.entry.name.toLowerCase().startsWith(needle)) return index;
  }
  return -1;
}

/** The layer a file's role puts it in, for the tree's colored dots (tests, config, docs and
 * other files have none). */
export function layerOfRole(role: string | null | undefined): Layer | null {
  switch (role) {
    case "ui":
      return "ui";
    case "api":
      return "api";
    case "service":
      return "svc";
    case "db":
      return "db";
    default:
      return null;
  }
}
