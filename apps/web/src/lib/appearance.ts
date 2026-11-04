/**
 * Appearance (10.7.1): how this browser shows repo-sensei, besides its theme (next-themes): an
 * accent hue and a contrast level for the generated palette (lib/theme/generate.ts), the density,
 * and whether motion and transparency are reduced whatever the system says.
 *
 * Kept in localStorage, with the palette's CSS worked out once when saved: the boot script
 * (APPEARANCE_BOOT_SCRIPT, in the root layout's head) applies it before the first paint, so a
 * page never flashes the default palette first.
 */
import { generateTheme, themeToCss, type ThemeBase } from "@/lib/theme/generate";

export type Density = "comfortable" | "compact";
export type Override = "system" | "reduce";

export interface Appearance {
  /** Accent hue in degrees, or null for the neutral accent. */
  accentHue: number | null;
  /** 0: the default; 1: high contrast. */
  contrast: 0 | 1;
  density: Density;
  motion: Override;
  transparency: Override;
}

export const DEFAULT_APPEARANCE: Appearance = {
  accentHue: null,
  contrast: 0,
  density: "comfortable",
  motion: "system",
  transparency: "system",
};

export const APPEARANCE_KEY = "sensei.appearance";
/** The palette's CSS for the saved choice ("" for the default palette). */
export const APPEARANCE_CSS_KEY = "sensei.appearance.css";
export const APPEARANCE_STYLE_ID = "sensei-appearance";

/** Accent hues offered, as names people know them by. */
export const ACCENT_HUES: readonly { label: string; hue: number | null }[] = [
  { label: "Neutral", hue: null },
  { label: "Blue", hue: 255 },
  { label: "Violet", hue: 295 },
  { label: "Pink", hue: 350 },
  { label: "Orange", hue: 55 },
  { label: "Green", hue: 150 },
  { label: "Teal", hue: 195 },
];

/** What was saved, whatever shape it was kept in (anything unreadable is the default). */
export function readAppearance(raw: string | null): Appearance {
  let found: unknown;
  try {
    found = raw ? JSON.parse(raw) : {};
  } catch {
    return DEFAULT_APPEARANCE;
  }
  if (typeof found !== "object" || found === null) return DEFAULT_APPEARANCE;
  const value = found as Record<string, unknown>;
  const hue = value.accentHue;
  return {
    accentHue:
      typeof hue === "number" && Number.isFinite(hue) && hue >= 0 && hue < 360 ? hue : null,
    contrast: value.contrast === 1 ? 1 : 0,
    density: value.density === "compact" ? "compact" : "comfortable",
    motion: value.motion === "reduce" ? "reduce" : "system",
    transparency: value.transparency === "reduce" ? "reduce" : "system",
  };
}

/** The palette's CSS for both bases (none for the default palette). Doubled attribute
 * selectors outrank the stylesheet's, wherever it loads, and reach nested theme regions too. */
export function appearanceCss(appearance: Appearance): string {
  if (appearance.accentHue === null && appearance.contrast === 0) return "";
  return (["dark", "light"] as const satisfies readonly ThemeBase[])
    .map((base) => {
      const tokens = generateTheme({
        base,
        accentHue: appearance.accentHue,
        contrast: appearance.contrast,
      });
      return `[data-theme="${base}"][data-theme]{${themeToCss(tokens)}}`;
    })
    .join("");
}

/** The document's attributes for the choice (the palette is its style element). */
export function applyAppearance(root: HTMLElement, appearance: Appearance, css: string): void {
  root.dataset.density = appearance.density;
  root.dataset.motion = appearance.motion;
  root.dataset.transparency = appearance.transparency;
  let style = document.getElementById(APPEARANCE_STYLE_ID);
  if (!css) {
    style?.remove();
    return;
  }
  if (style === null) {
    style = document.createElement("style");
    style.id = APPEARANCE_STYLE_ID;
    document.head.append(style);
  }
  style.textContent = css;
}

export const APPEARANCE_EVENT = "sensei:appearance";

/** Saved, and applied now (and told to whatever follows it, as the motion setup does). */
export function saveAppearance(appearance: Appearance): void {
  const css = appearanceCss(appearance);
  localStorage.setItem(APPEARANCE_KEY, JSON.stringify(appearance));
  if (css) localStorage.setItem(APPEARANCE_CSS_KEY, css);
  else localStorage.removeItem(APPEARANCE_CSS_KEY);
  applyAppearance(document.documentElement, appearance, css);
  window.dispatchEvent(new Event(APPEARANCE_EVENT));
}

/** The saved choice as an external store, for useSyncExternalStore: told when saved here or
 * in another tab; its snapshot is what was saved (none on the server: the default). */
export function subscribeAppearance(listener: () => void): () => void {
  window.addEventListener(APPEARANCE_EVENT, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(APPEARANCE_EVENT, listener);
    window.removeEventListener("storage", listener);
  };
}

export function savedAppearance(): string | null {
  return localStorage.getItem(APPEARANCE_KEY);
}

export function serverAppearance(): string | null {
  return null;
}

/** Before the first paint: the saved attributes and palette. The palette is kept as CSS made
 * by this code (from numbers it checked), and is only put in a style element's text. */
export const APPEARANCE_BOOT_SCRIPT = `(function(){try{var a=JSON.parse(localStorage.getItem(${JSON.stringify(
  APPEARANCE_KEY,
)})||"{}")||{},r=document.documentElement;r.dataset.density=a.density==="compact"?"compact":"comfortable";r.dataset.motion=a.motion==="reduce"?"reduce":"system";r.dataset.transparency=a.transparency==="reduce"?"reduce":"system";var c=localStorage.getItem(${JSON.stringify(
  APPEARANCE_CSS_KEY,
)});if(c&&c.indexOf("<")<0){var s=document.createElement("style");s.id=${JSON.stringify(
  APPEARANCE_STYLE_ID,
)};s.textContent=c;document.head.appendChild(s)}}catch(e){}})();`;
