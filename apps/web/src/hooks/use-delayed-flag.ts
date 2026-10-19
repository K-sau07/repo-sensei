import { useEffect, useRef, useState } from "react";

export interface DelayedFlagOptions {
  /** Wait this long before showing, so fast operations never flash an indicator. */
  delayMs?: number;
  /** Once shown, stay visible at least this long, so the indicator never blinks. */
  minVisibleMs?: number;
}

/**
 * Turns a raw "busy" flag into a calm "show the indicator" flag: spinners appear after 200ms and
 * stay for at least 400ms, so quick work never flickers a spinner.
 */
export function useDelayedFlag(
  active: boolean,
  { delayMs = 200, minVisibleMs = 400 }: DelayedFlagOptions = {},
): boolean {
  const [visible, setVisible] = useState(false);
  const shownAt = useRef<number | null>(null);

  useEffect(() => {
    if (active) {
      if (visible) return;
      const timer = setTimeout(() => {
        shownAt.current = performance.now();
        setVisible(true);
      }, delayMs);
      return () => {
        clearTimeout(timer);
      };
    }
    if (!visible) return;
    const elapsed = performance.now() - (shownAt.current ?? 0);
    const timer = setTimeout(
      () => {
        shownAt.current = null;
        setVisible(false);
      },
      Math.max(0, minVisibleMs - elapsed),
    );
    return () => {
      clearTimeout(timer);
    };
  }, [active, visible, delayMs, minVisibleMs]);

  return visible;
}
