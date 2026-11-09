import { useCallback, useSyncExternalStore } from "react";

/** The app's breakpoints, as Tailwind's are, in rem: `md` from 48rem, `lg` from 64rem (768px
 * and 1024px at the usual 16px; wider where someone set a larger default font, as Tailwind's
 * classes then are too). */
export const MD = "(min-width: 48rem)";
export const LG = "(min-width: 64rem)";

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
