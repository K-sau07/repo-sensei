"use client";

import { useRef } from "react";

import { SIDEBAR_MAX, SIDEBAR_MIN, useShellStore } from "./shell-store";

const STEP = 16;

/** The drag handle on the sidebar's edge. Also works with the keyboard (arrows, Home, End). */
export function SidebarResizer() {
  const width = useShellStore((state) => state.sidebarWidth);
  const setWidth = useShellStore((state) => state.setSidebarWidth);
  const drag = useRef<{ startX: number; startWidth: number } | null>(null);

  return (
    // A focusable separator with a value is the WAI-ARIA "window splitter": an interactive widget.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sidebar"
      aria-valuemin={SIDEBAR_MIN}
      aria-valuemax={SIDEBAR_MAX}
      aria-valuenow={width}
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
      className="absolute inset-y-0 -right-1 z-10 w-2 cursor-col-resize outline-none after:absolute after:inset-y-0 after:left-1/2 after:w-px after:-translate-x-1/2 after:bg-transparent hover:after:bg-line-strong focus-visible:after:w-0.5 focus-visible:after:bg-ring in-data-[sidebar=collapsed]:hidden"
      onPointerDown={(event) => {
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = { startX: event.clientX, startWidth: width };
      }}
      onPointerMove={(event) => {
        if (!drag.current) return;
        setWidth(drag.current.startWidth + event.clientX - drag.current.startX);
      }}
      onPointerUp={() => {
        drag.current = null;
      }}
      onKeyDown={(event) => {
        const next = {
          ArrowLeft: width - STEP,
          ArrowRight: width + STEP,
          Home: SIDEBAR_MIN,
          End: SIDEBAR_MAX,
        }[event.key];
        if (next !== undefined) {
          event.preventDefault();
          setWidth(next);
        }
      }}
    />
  );
}
