/**
 * Graphite Signal theme generator.
 *
 * Three inputs, as in Linear's theme editor: a base (dark or light), an optional accent hue and
 * a contrast level. With no accent and zero contrast the output is exactly the default palette in
 * `src/styles/tokens.css` (a test keeps the two in sync). `--signal` never changes: it is reserved
 * for "live / AI / verified".
 *
 * Every color is an in-gamut sRGB color written in OKLCH, so browsers never have to gamut-map.
 */
import {
  clampChroma,
  modeLrgb,
  modeOklab,
  modeOklch,
  modeRgb,
  useMode as registerMode,
} from "culori/fn";

registerMode(modeRgb);
registerMode(modeLrgb);
registerMode(modeOklab);
registerMode(modeOklch);

export type ThemeBase = "dark" | "light";

export interface ThemeInput {
  base: ThemeBase;
  /** Accent hue in degrees (0-360) for buttons, links and focus; null keeps the neutral accent. */
  accentHue?: number | null;
  /** 0 = default, 1 = high contrast. Moves text and borders further from the surfaces. */
  contrast?: number;
}

export const COLOR_TOKENS = [
  "bg",
  "surface-1",
  "surface-2",
  "surface-3",
  "line",
  "line-strong",
  "text",
  "text-muted",
  "text-faint",
  "signal",
  "layer-ui",
  "layer-api",
  "layer-svc",
  "layer-db",
  "danger",
  "warn",
  "ok",
  "brand",
  "brand-fg",
] as const;

export type ColorToken = (typeof COLOR_TOKENS)[number];
export type ThemeTokens = Record<ColorToken, string>;

/** Text-like tokens, each with the minimum APCA Lc it must reach on every surface. */
export const TEXT_CONTRAST_MINIMUMS: Partial<Record<ColorToken, number>> = {
  text: 90,
  "text-muted": 60,
  "text-faint": 40,
  signal: 60,
  "layer-ui": 60,
  "layer-api": 60,
  "layer-svc": 60,
  "layer-db": 60,
  danger: 60,
  warn: 60,
  ok: 60,
  brand: 60,
};

export const SURFACE_TOKENS = ["bg", "surface-1", "surface-2", "surface-3"] as const;

interface Lch {
  l: number;
  c: number;
  h: number;
  alpha?: number;
}

const HUE = 260; // the cool graphite tint shared by surfaces, text and lines

/**
 * Fixed hues: the four layer colors keep matched lightness so no layer looks more important.
 * Chroma values are already the largest that stays inside sRGB at that lightness and hue.
 */
const PALETTES: Record<ThemeBase, Record<Exclude<ColorToken, "brand" | "brand-fg">, Lch>> = {
  dark: {
    bg: { l: 0.16, c: 0.005, h: HUE },
    "surface-1": { l: 0.2, c: 0.006, h: HUE },
    "surface-2": { l: 0.24, c: 0.007, h: HUE },
    "surface-3": { l: 0.28, c: 0.008, h: HUE },
    line: { l: 1, c: 0, h: 0, alpha: 0.08 },
    "line-strong": { l: 1, c: 0, h: 0, alpha: 0.14 },
    text: { l: 0.96, c: 0.003, h: HUE },
    "text-muted": { l: 0.78, c: 0.01, h: HUE },
    "text-faint": { l: 0.67, c: 0.01, h: HUE },
    signal: { l: 0.84, c: 0.15, h: 170 },
    "layer-ui": { l: 0.81, c: 0.099, h: 250 },
    "layer-api": { l: 0.81, c: 0.14, h: 75 },
    "layer-svc": { l: 0.81, c: 0.11, h: 140 },
    "layer-db": { l: 0.81, c: 0.12, h: 310 },
    danger: { l: 0.8, c: 0.114, h: 25 },
    warn: { l: 0.84, c: 0.15, h: 85 },
    ok: { l: 0.81, c: 0.15, h: 150 },
  },
  light: {
    bg: { l: 0.985, c: 0.002, h: HUE },
    "surface-1": { l: 0.97, c: 0.003, h: HUE },
    "surface-2": { l: 1, c: 0, h: 0 },
    "surface-3": { l: 0.94, c: 0.005, h: HUE },
    line: { l: 0.22, c: 0.01, h: HUE, alpha: 0.12 },
    "line-strong": { l: 0.22, c: 0.01, h: HUE, alpha: 0.2 },
    text: { l: 0.22, c: 0.01, h: HUE },
    "text-muted": { l: 0.42, c: 0.012, h: HUE },
    "text-faint": { l: 0.5, c: 0.012, h: HUE },
    signal: { l: 0.5, c: 0.098, h: 170 },
    "layer-ui": { l: 0.5, c: 0.141, h: 250 },
    "layer-api": { l: 0.5, c: 0.105, h: 75 },
    "layer-svc": { l: 0.5, c: 0.12, h: 140 },
    "layer-db": { l: 0.5, c: 0.17, h: 310 },
    danger: { l: 0.52, c: 0.19, h: 25 },
    warn: { l: 0.51, c: 0.104, h: 85 },
    ok: { l: 0.5, c: 0.13, h: 150 },
  },
};

/** How far each text tier and border moves at contrast = 1 (signed for the base). */
const CONTRAST_SHIFT: Record<
  ThemeBase,
  Partial<Record<ColorToken, { l?: number; alpha?: number }>>
> = {
  dark: {
    text: { l: 0.03 },
    "text-muted": { l: 0.1 },
    "text-faint": { l: 0.12 },
    line: { alpha: 0.1 },
    "line-strong": { alpha: 0.16 },
  },
  light: {
    text: { l: -0.12 },
    "text-muted": { l: -0.14 },
    "text-faint": { l: -0.16 },
    line: { alpha: 0.13 },
    "line-strong": { alpha: 0.2 },
  },
};

/** Accent lightness and chroma per base; chroma is reduced further if the hue needs it. */
const ACCENT: Record<ThemeBase, { l: number; c: number }> = {
  dark: { l: 0.81, c: 0.13 },
  light: { l: 0.5, c: 0.15 },
};

function round(value: number, digits = 3): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

/** The largest in-gamut chroma at this lightness and hue, never above the requested one. */
function inGamut(color: Lch): Lch {
  if (color.c === 0) return color;
  const clamped = clampChroma({ mode: "oklch", l: color.l, c: color.c, h: color.h }, "oklch");
  // Round down so the written value stays inside the gamut.
  const c = Math.floor(clamped.c * 1000) / 1000;
  return { ...color, c };
}

export function formatOklch({ l, c, h, alpha }: Lch): string {
  const parts = `${round(l)} ${round(c)} ${c === 0 ? 0 : round(h, 1)}`;
  return alpha === undefined ? `oklch(${parts})` : `oklch(${parts} / ${round(alpha)})`;
}

export function generateTheme({ base, accentHue = null, contrast = 0 }: ThemeInput): ThemeTokens {
  const amount = clamp01(contrast);
  const palette = PALETTES[base];
  const tokens = {} as ThemeTokens;

  for (const [name, color] of Object.entries(palette) as [keyof typeof palette, Lch][]) {
    const shift = CONTRAST_SHIFT[base][name];
    const adjusted: Lch = {
      ...color,
      l: clamp01(color.l + (shift?.l ?? 0) * amount),
      ...(color.alpha === undefined
        ? {}
        : { alpha: clamp01(color.alpha + (shift?.alpha ?? 0) * amount) }),
    };
    tokens[name] = formatOklch(inGamut(adjusted));
  }

  if (accentHue === null) {
    // Neutral accent: primary actions use the text color, like Vercel and Linear defaults.
    tokens.brand = tokens.text;
    tokens["brand-fg"] = tokens.bg;
  } else {
    const hue = ((accentHue % 360) + 360) % 360;
    tokens.brand = formatOklch(inGamut({ ...ACCENT[base], h: hue }));
    tokens["brand-fg"] = base === "dark" ? tokens.bg : formatOklch({ l: 1, c: 0, h: 0 });
  }
  return tokens;
}

/** CSS declarations for inline overrides, e.g. on `<html style>` from appearance settings. */
export function themeToCss(tokens: ThemeTokens): string {
  return COLOR_TOKENS.map((name) => `--${name}: ${tokens[name]};`).join(" ");
}
