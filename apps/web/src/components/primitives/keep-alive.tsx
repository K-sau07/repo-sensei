"use client";

import { Activity, useState, type ReactNode } from "react";

/** Panels that keep their state while hidden: only `active` is shown, the others stay mounted
 * in a hidden `<Activity>` (effects paused, state kept), so switching back is instant and
 * nothing is lost. Panels mount the first time they're shown. */
export function KeepAlive({
  active,
  panels,
}: {
  active: string;
  panels: Record<string, () => ReactNode>;
}) {
  const visited = useVisited(active);
  return (
    <>
      {Object.entries(panels).map(([key, render]) =>
        visited.has(key) ? (
          <Activity key={key} mode={key === active ? "visible" : "hidden"}>
            {render()}
          </Activity>
        ) : null,
      )}
    </>
  );
}

function useVisited(active: string): ReadonlySet<string> {
  const [visited, setVisited] = useState<ReadonlySet<string>>(() => new Set([active]));
  if (!visited.has(active)) {
    // Adjusting state while rendering (React's pattern for derived state): no extra pass.
    const next = new Set(visited);
    next.add(active);
    setVisited(next);
    return next;
  }
  return visited;
}
