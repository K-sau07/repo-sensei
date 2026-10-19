/**
 * Contrast measurements for design-time checks (tests and Storybook only, never shipped):
 * APCA Lc via the reference `apca-w3` package, and the WCAG 2 contrast ratio.
 */
import { APCAcontrast, sRGBtoY } from "apca-w3";
import {
  formatRgb,
  modeOklch,
  modeRgb,
  parse,
  useMode as registerMode,
  wcagContrast,
} from "culori/fn";

registerMode(modeRgb);
registerMode(modeOklch);

function rgb255(color: string): [number, number, number] {
  const parsed = parse(color);
  if (!parsed) throw new Error(`not a color: ${color}`);
  const match = /rgb\((\d+), (\d+), (\d+)\)/.exec(formatRgb(parsed));
  if (!match) throw new Error(`cannot convert ${color} to sRGB`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

/** Absolute APCA lightness contrast (Lc) of `text` on `background`. */
export function apca(text: string, background: string): number {
  return Math.abs(Number(APCAcontrast(sRGBtoY(rgb255(text)), sRGBtoY(rgb255(background)))));
}

/** WCAG 2.x contrast ratio (1-21). */
export function wcag(text: string, background: string): number {
  return wcagContrast(text, background);
}
