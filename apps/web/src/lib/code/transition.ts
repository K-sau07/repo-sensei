/**
 * Opening code from a search result with a View Transition: the card morphs into the code viewer
 * showing its lines. The card takes the transition's name as it's clicked, and the viewer showing
 * those lines (`data-code-view`) has it once it's on screen. Without the View Transition API, or
 * with reduced motion, it's a plain navigation.
 */
import type { LineRange } from "./lines";

/** The name both sides take (styled in globals.css). */
export const OPEN_CODE_TRANSITION = "open-code";

/** How long the old page may stay frozen waiting for the viewer; then the new page shows as it
 * is (the morph is a nicety, never a wait). */
const WAIT_MS = 500;

/** What the viewer's `data-code-view` says, for the lines it shows. */
export function codeViewKey(path: string, lines: LineRange | null): string | undefined {
  return lines ? `${path}:${String(lines.start)}-${String(lines.end)}` : undefined;
}

function shown(key: string): HTMLElement | null {
  for (const element of document.querySelectorAll<HTMLElement>("[data-code-view]")) {
    if (element.dataset.codeView === key && element.getClientRects().length > 0) return element;
  }
  return null;
}

/** The viewer showing `key` once it's on screen (null if it isn't by the deadline). */
function viewerShown(key: string): Promise<HTMLElement | null> {
  return new Promise((resolve) => {
    const started = performance.now();
    const check = () => {
      const viewer = shown(key);
      if (viewer || performance.now() - started > WAIT_MS) resolve(viewer);
      else requestAnimationFrame(check);
    };
    requestAnimationFrame(check);
  });
}

export function canTransition(): boolean {
  return (
    typeof document !== "undefined" &&
    typeof document.startViewTransition === "function" &&
    !window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** Navigate (`go`) with the morph from `source` to the viewer showing `key`. */
export function openCode(go: () => void, source: HTMLElement, key: string): void {
  if (!canTransition()) {
    go();
    return;
  }
  // The name is on each side only while the transition runs, so nothing else ever carries it.
  source.style.viewTransitionName = OPEN_CODE_TRANSITION;
  let viewer: HTMLElement | null = null;
  const transition = document.startViewTransition(async () => {
    source.style.viewTransitionName = "";
    go();
    viewer = await viewerShown(key);
    if (viewer) viewer.style.viewTransitionName = OPEN_CODE_TRANSITION;
  });
  transition.finished
    .catch(() => {
      // Skipped (another transition, a hidden tab): the page is there all the same.
    })
    .finally(() => {
      source.style.viewTransitionName = "";
      if (viewer) viewer.style.viewTransitionName = "";
    });
}

/** Navigate (`go`) to the viewer showing `key` with a cross-fade: going to a definition, or back
 * from one. */
export function crossToCode(go: () => void, key: string): void {
  if (!canTransition()) {
    go();
    return;
  }
  const transition = document.startViewTransition(async () => {
    go();
    await viewerShown(key);
  });
  transition.finished.catch(() => {
    // Skipped: the page is there all the same.
  });
}
