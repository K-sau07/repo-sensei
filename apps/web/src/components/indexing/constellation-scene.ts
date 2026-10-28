"use client";

/**
 * What the constellation's two renderers share: their props, and the theme's colors read from
 * the design tokens (three.js can't read CSS variables, nor OKLCH; and the built stylesheet may
 * hold the tokens in another notation, such as `lab()`, so every CSS one is understood).
 */
import {
  converter,
  modeLab,
  modeLch,
  modeLrgb,
  modeOklab,
  modeOklch,
  modeRgb,
  parse,
  useMode as registerMode,
} from "culori/fn";
import { useEffect, useState } from "react";

import type { Layer, MotionMode, Sky } from "@/lib/constellation";

for (const mode of [modeRgb, modeLrgb, modeLab, modeLch, modeOklab, modeOklch]) {
  registerMode(mode);
}
const toRgb = converter("rgb");

/** sRGB, 0..1. */
export type Rgb = [number, number, number];

export interface SkyColors {
  layers: Record<Layer, Rgb>;
  signal: Rgb;
  edge: Rgb;
  background: Rgb;
}

/** A point under the pointer: its index and where it is in the scene's box (px). */
export interface Hit {
  index: number;
  x: number;
  y: number;
}

export interface SceneProps {
  sky: Sky;
  colors: SkyColors;
  motion: MotionMode;
  /** Turn slowly (live motion only). */
  drift: boolean;
  /** On screen and the page visible: otherwise no frames at all. */
  active: boolean;
  /** A directory whose cluster to light up. */
  highlight: number | null;
  /** A trace's stars, in its order (3.15): lit, and joined by its code edges. */
  trace: readonly number[];
  label: string;
  onHover: (hit: Hit | null) => void;
  onSelect: (hit: Hit | null) => void;
  /** Called for every frame drawn, with the time since the last one (ms). */
  onFrame?: (ms: number) => void;
}

const TOKENS: Record<Layer, string> = {
  ui: "--layer-ui",
  api: "--layer-api",
  svc: "--layer-svc",
  db: "--layer-db",
  other: "--text-faint",
};

function read(style: CSSStyleDeclaration, name: string, fallback: Rgb): Rgb {
  const parsed = parse(style.getPropertyValue(name).trim());
  const rgb = parsed ? toRgb(parsed) : undefined;
  if (!rgb) return fallback;
  const clamp = (v: number) => Math.min(1, Math.max(0, v));
  return [clamp(rgb.r), clamp(rgb.g), clamp(rgb.b)];
}

/** The current theme's colors, from `element`'s computed tokens. */
export function readColors(element: Element): SkyColors {
  const style = getComputedStyle(element);
  const grey: Rgb = [0.6, 0.6, 0.6];
  return {
    layers: {
      ui: read(style, TOKENS.ui, grey),
      api: read(style, TOKENS.api, grey),
      svc: read(style, TOKENS.svc, grey),
      db: read(style, TOKENS.db, grey),
      other: read(style, TOKENS.other, grey),
    },
    signal: read(style, "--signal", [0.24, 0.88, 0.71]),
    edge: read(style, "--text-faint", grey),
    background: read(style, "--bg", [0.05, 0.05, 0.06]),
  };
}

/** The theme's colors, read again when the theme changes (`data-theme` on <html>). */
export function useSkyColors(): SkyColors | null {
  const [colors, setColors] = useState<SkyColors | null>(null);
  useEffect(() => {
    const root = document.documentElement;
    const update = () => {
      setColors(readColors(root));
    };
    update();
    const observer = new MutationObserver(update);
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme", "class", "style"] });
    return () => {
      observer.disconnect();
    };
  }, []);
  return colors;
}
