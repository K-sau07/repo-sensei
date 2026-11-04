/** When heavy, optional work (WebGL scenes) may start, and whether it may draw: after the browser
 * is first idle, and only while its element is on screen in a visible page. */
import { useEffect, useState } from "react";

const IDLE_TIMEOUT_MS = 1500;

/** False until the browser is first idle after mounting (or IDLE_TIMEOUT_MS has passed). */
export function useIdle(): boolean {
  const [idle, setIdle] = useState(false);
  useEffect(() => {
    if (typeof requestIdleCallback === "function") {
      const id = requestIdleCallback(
        () => {
          setIdle(true);
        },
        { timeout: IDLE_TIMEOUT_MS },
      );
      return () => {
        cancelIdleCallback(id);
      };
    }
    const id = setTimeout(() => {
      setIdle(true);
    }, 0);
    return () => {
      clearTimeout(id);
    };
  }, []);
  return idle;
}

/** Whether the element is on screen and the page visible (no frames are drawn otherwise). */
export function useActive(element: React.RefObject<HTMLElement | null>): boolean {
  const [onScreen, setOnScreen] = useState(true);
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const target = element.current;
    if (!target || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => {
      setOnScreen(entry?.isIntersecting ?? true);
    });
    observer.observe(target);
    return () => {
      observer.disconnect();
    };
  }, [element]);
  useEffect(() => {
    const update = () => {
      setVisible(document.visibilityState !== "hidden");
    };
    update();
    document.addEventListener("visibilitychange", update);
    return () => {
      document.removeEventListener("visibilitychange", update);
    };
  }, []);
  return onScreen && visible;
}
