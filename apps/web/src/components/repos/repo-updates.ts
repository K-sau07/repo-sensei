/**
 * New indexes (5.6): each repository's latest `repo_updated` from the org stream (a ref's index
 * moved on to a new commit), for the "New commits indexed" banner of its open pages.
 */
import { create } from "zustand";

export interface RepoUpdate {
  ref: string;
  sha: string;
  /** The paths read again since the last index (capped); null: read in full, none known. */
  changedFiles: readonly string[] | null;
  changedCount: number | null;
  /** When it came (`Date.now()`): a page shows only those that came while it was open. */
  at: number;
}

interface RepoUpdates {
  updates: Readonly<Record<string, RepoUpdate>>;
  note: (repoId: string, update: RepoUpdate) => void;
  dismiss: (repoId: string) => void;
}

export const useRepoUpdates = create<RepoUpdates>((set) => ({
  updates: {},
  note: (repoId, update) => {
    set((state) => ({ updates: { ...state.updates, [repoId]: update } }));
  },
  dismiss: (repoId) => {
    set((state) => {
      const { [repoId]: _gone, ...rest } = state.updates;
      return { updates: rest };
    });
  },
}));

/** The banner's words: "New commits indexed on main · 3 files changed". */
export function updateLabel(update: RepoUpdate): string {
  const n = update.changedCount;
  const files = n === null ? "" : ` · ${String(n)} file${n === 1 ? "" : "s"} changed`;
  return `New commits indexed on ${update.ref}${files}`;
}
