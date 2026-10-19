/**
 * Motion tokens, mirrored from `src/styles/tokens.css` for Motion components.
 * A test keeps both in sync. Motion takes durations in seconds.
 */

export const durationMs = {
  instant: 0, // frequent actions (⌘K, keyboard list navigation, context menus) never animate
  fast: 120, // hover, press, toggles
  base: 200, // popovers, dropdowns, tab content
  slow: 320, // panels, drawers, view transitions
  signature: 700, // the Trace Beam and other rare moments
} as const;

export type Duration = keyof typeof durationMs;

export const duration = Object.fromEntries(
  Object.entries(durationMs).map(([name, ms]) => [name, ms / 1000]),
) as Record<Duration, number>;

export const ease = {
  /** Default for UI feedback. */
  out: [0.23, 1, 0.32, 1],
  /** Movement across the screen. */
  inOut: [0.65, 0, 0.35, 1],
} as const satisfies Record<string, readonly [number, number, number, number]>;

/** Drag, canvas and panel resize. */
export const spring = { type: "spring", stiffness: 380, damping: 32 } as const;

/** Buttons press down to this scale. */
export const pressScale = 0.97;
