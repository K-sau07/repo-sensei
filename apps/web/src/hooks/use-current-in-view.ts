import { useCallback } from "react";

/** A ref for a row of links that scrolls sideways on a narrow screen: the current one
 * (`aria-current`) is scrolled into view, so the page you're on is never off the edge. */
export function useCurrentInView(key: string) {
  return useCallback(
    (row: HTMLElement | null) => {
      row
        ?.querySelector("[aria-current=page]")
        ?.scrollIntoView({ block: "nearest", inline: "nearest" });
    },
    // A new ref each page, so it runs again when the current link changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key],
  );
}
