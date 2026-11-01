"use client";

/**
 * The graph explorer's colors, read from the design tokens as hex (WebGL reads neither CSS
 * variables nor OKLCH; the built stylesheet may hold the tokens in another notation, so every CSS
 * one is understood), and read again when the theme changes.
 */
import {
  converter,
  formatHex,
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

import type { Tint } from "@/lib/api/deps";

for (const mode of [modeRgb, modeLrgb, modeLab, modeLch, modeOklab, modeOklch]) {
  registerMode(mode);
}
const toRgb = converter("rgb");

export interface GraphColors {
  tints: Record<Tint, string>;
  /** An edge at rest, and one lit (hovered, on a path, found). */
  edge: string;
  lit: string;
  /** What closes a cycle. */
  cycle: string;
  label: string;
  background: string;
}

const TOKENS: Record<Tint, string> = {
  ui: "--layer-ui",
  api: "--layer-api",
  svc: "--layer-svc",
  db: "--layer-db",
  other: "--text-faint",
};

function hex(style: CSSStyleDeclaration, name: string, fallback: string): string {
  const parsed = parse(style.getPropertyValue(name).trim());
  const rgb = parsed ? toRgb(parsed) : undefined;
  if (!rgb) return fallback;
  const clamp = (v: number) => Math.min(1, Math.max(0, v));
  return formatHex({ mode: "rgb", r: clamp(rgb.r), g: clamp(rgb.g), b: clamp(rgb.b) });
}

export function readGraphColors(element: Element): GraphColors {
  const style = getComputedStyle(element);
  const grey = "#999999";
  return {
    tints: {
      ui: hex(style, TOKENS.ui, grey),
      api: hex(style, TOKENS.api, grey),
      svc: hex(style, TOKENS.svc, grey),
      db: hex(style, TOKENS.db, grey),
      other: hex(style, TOKENS.other, grey),
    },
    edge: hex(style, "--text-faint", grey),
    lit: hex(style, "--signal", "#3ee0b5"),
    cycle: hex(style, "--danger", "#f07070"),
    label: hex(style, "--text", "#e8e8e8"),
    background: hex(style, "--bg", "#0d0d0f"),
  };
}

export function useGraphColors(): GraphColors | null {
  const [colors, setColors] = useState<GraphColors | null>(null);
  useEffect(() => {
    const root = document.documentElement;
    const update = () => {
      setColors(readGraphColors(root));
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
