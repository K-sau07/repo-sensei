/**
 * Keyboard shortcut notation. Shortcuts are written platform-neutrally, e.g. "mod+k",
 * "shift+mod+p", "?" or a sequence "g r" (press g, then r); `mod` is ⌘ on Apple and Ctrl elsewhere.
 */

export type Platform = "apple" | "other";

export interface KeyLabel {
  /** What is printed on the key cap. */
  symbol: string;
  /** What a screen reader says. */
  name: string;
}

const APPLE: Record<string, KeyLabel> = {
  mod: { symbol: "⌘", name: "Command" },
  ctrl: { symbol: "⌃", name: "Control" },
  alt: { symbol: "⌥", name: "Option" },
  shift: { symbol: "⇧", name: "Shift" },
  enter: { symbol: "↵", name: "Return" },
  backspace: { symbol: "⌫", name: "Delete" },
};

const OTHER: Record<string, KeyLabel> = {
  mod: { symbol: "Ctrl", name: "Control" },
  ctrl: { symbol: "Ctrl", name: "Control" },
  alt: { symbol: "Alt", name: "Alt" },
  shift: { symbol: "Shift", name: "Shift" },
  enter: { symbol: "Enter", name: "Enter" },
  backspace: { symbol: "Backspace", name: "Backspace" },
};

const SHARED: Record<string, KeyLabel> = {
  esc: { symbol: "Esc", name: "Escape" },
  tab: { symbol: "Tab", name: "Tab" },
  space: { symbol: "Space", name: "Space" },
  up: { symbol: "↑", name: "Up arrow" },
  down: { symbol: "↓", name: "Down arrow" },
  left: { symbol: "←", name: "Left arrow" },
  right: { symbol: "→", name: "Right arrow" },
  "?": { symbol: "?", name: "Question mark" },
  "/": { symbol: "/", name: "Slash" },
  ".": { symbol: ".", name: "Period" },
};

// Modifiers print in the platform's conventional order.
const ORDER = ["ctrl", "alt", "shift", "mod"];

function label(key: string, platform: Platform): KeyLabel {
  const lower = key.toLowerCase();
  const known = (platform === "apple" ? APPLE : OTHER)[lower] ?? SHARED[lower];
  if (known) return known;
  const upper =
    key.length === 1 ? key.toUpperCase() : `${key.charAt(0).toUpperCase()}${key.slice(1)}`;
  return { symbol: upper, name: upper };
}

/** Each step of a shortcut (one step, or several for a sequence) as its key labels. */
export function formatShortcut(shortcut: string, platform: Platform): KeyLabel[][] {
  return shortcut
    .trim()
    .split(/\s+/)
    .map((step) => {
      const keys = step === "+" ? ["+"] : step.split("+").filter(Boolean);
      const modifiers = keys
        .filter((key) => ORDER.includes(key.toLowerCase()))
        .sort((a, b) => ORDER.indexOf(a.toLowerCase()) - ORDER.indexOf(b.toLowerCase()));
      const rest = keys.filter((key) => !ORDER.includes(key.toLowerCase()));
      return [...modifiers, ...rest].map((key) => label(key, platform));
    });
}

/** "Command K", "Control Shift P", "G then R". */
export function describeShortcut(shortcut: string, platform: Platform): string {
  return formatShortcut(shortcut, platform)
    .map((step) => step.map((key) => key.name).join(" "))
    .join(" then ");
}

export function detectPlatform(navigatorLike: Pick<Navigator, "platform" | "userAgent">): Platform {
  const source = `${navigatorLike.platform} ${navigatorLike.userAgent}`;
  return /Mac|iPhone|iPad|iPod/i.test(source) ? "apple" : "other";
}
