import { useCallback, useSyncExternalStore } from "react";

/** The app's breakpoints (Tailwind's): `md` from 768px, `lg` from 1024px. */
export const MD = "(min-width: 768px)";
export const LG = "(min-width: 1024px)";

/** Whether `query` matches now (taken as matching during server rendering: the desktop
 * layout, which a narrower screen replaces once it hydrates). */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => {
        list.removeEventListener("change", onChange);
      };
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(query).matches,
    () => true,
  );
}
