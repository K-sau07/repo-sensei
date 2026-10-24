/**
 * Syntax-highlighted lines without an editor, for code previews: the viewer's own grammars
 * (@codemirror/language-data, each loaded on first use) and its colors (SYNTAX_STYLES), as
 * tokens to render as text. Loaded on demand, like the viewer.
 */
import { LanguageDescription } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { highlightCode, tagHighlighter } from "@lezer/highlight";
import type { CSSProperties } from "react";

import { SYNTAX_STYLES } from "@/components/code/codemirror-theme";
import type { Token } from "@/lib/search/highlight";

const highlighter = tagHighlighter(
  SYNTAX_STYLES.map((style, index) => ({ tag: style.tag, class: `s${String(index)}` })),
);

/** Each token tag's style. */
export const TOKEN_STYLES: Readonly<Record<string, CSSProperties>> = Object.fromEntries(
  SYNTAX_STYLES.map((style, index) => [
    `s${String(index)}`,
    {
      ...(typeof style.color === "string" ? { color: style.color } : {}),
      ...(typeof style.fontStyle === "string" ? { fontStyle: style.fontStyle } : {}),
      ...(typeof style.fontWeight === "string" ? { fontWeight: style.fontWeight } : {}),
      ...(typeof style.textDecoration === "string" ? { textDecoration: style.textDecoration } : {}),
    } satisfies CSSProperties,
  ]),
);

async function parserFor(path: string, language: string | null) {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const found =
    LanguageDescription.matchFilename(languages, name) ??
    (language ? LanguageDescription.matchLanguageName(languages, language, false) : null);
  if (!found) return null;
  const support = await found.load();
  return support.language.parser;
}

/**
 * Lines `from` to `to` (1-based, inclusive) of `text` as syntax tokens, the whole file parsed so
 * a line inside a docstring or a template reads as one. Null when there's no grammar for it.
 */
export async function tokenizeLines(
  path: string,
  language: string | null,
  text: string,
  from: number,
  to: number,
): Promise<Token[][] | null> {
  const parser = await parserFor(path, language);
  if (!parser) return null;
  const starts = [0];
  for (let at = text.indexOf("\n"); at !== -1; at = text.indexOf("\n", at + 1)) {
    starts.push(at + 1);
  }
  const start = starts[from - 1];
  if (start === undefined) return null;
  const end = starts[to] === undefined ? text.length : (starts[to] ?? text.length) - 1;
  const tree = parser.parse(text);
  const lines: Token[][] = [[]];
  highlightCode(
    text,
    tree,
    highlighter,
    (code, classes) => {
      const first = classes.split(" ")[0];
      const tag = classes === "" ? null : (first ?? null);
      lines.at(-1)?.push({ text: code.replace(/\r$/, ""), tag });
    },
    () => {
      lines.push([]);
    },
    start,
    end,
  );
  return lines;
}
