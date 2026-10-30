import localFont from "next/font/local";

// Fonts are self-hosted from this folder (each with its OFL license) so the app, Storybook and
// tests all load them the same way. Fallback stacks live in globals.css (--font-sans, --font-mono).
// Only the UI font is preloaded: the code fonts (six files) load when code is first shown, so
// they don't hold up a page's first paint where there is little or none.

/** UI and headings: Geist Sans (variable, 100-900). */
export const geistSans = localFont({
  src: "./geist/Geist-Variable.woff2",
  weight: "100 900",
  variable: "--font-geist-sans",
  display: "swap",
});

/** Secondary code font, used where Commit Mono is missing a glyph. */
export const geistMono = localFont({
  src: "./geist/GeistMono-Variable.woff2",
  weight: "100 900",
  variable: "--font-geist-mono",
  display: "swap",
  preload: false,
});

/** Code: Commit Mono. */
export const commitMono = localFont({
  src: [
    { path: "./commit-mono/commit-mono-latin-400-normal.woff2", weight: "400", style: "normal" },
    { path: "./commit-mono/commit-mono-latin-400-italic.woff2", weight: "400", style: "italic" },
    { path: "./commit-mono/commit-mono-latin-500-normal.woff2", weight: "500", style: "normal" },
    { path: "./commit-mono/commit-mono-latin-600-normal.woff2", weight: "600", style: "normal" },
    { path: "./commit-mono/commit-mono-latin-700-normal.woff2", weight: "700", style: "normal" },
  ],
  variable: "--font-commit-mono",
  display: "swap",
  preload: false,
});

/** Class names that define --font-geist-sans, --font-geist-mono and --font-commit-mono. */
export const fontVariables = [geistSans.variable, geistMono.variable, commitMono.variable].join(
  " ",
);
