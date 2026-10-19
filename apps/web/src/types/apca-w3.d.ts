// apca-w3 ships without types; these cover the two functions we use.
declare module "apca-w3" {
  /** Lc for text luminance on background luminance (signed; positive = dark text on light). */
  export function APCAcontrast(
    textY: number,
    backgroundY: number,
    places?: number,
  ): number | string;
  /** Screen luminance (Y) of an sRGB color given as 0-255 channels. */
  export function sRGBtoY(rgb: readonly number[]): number;
}
