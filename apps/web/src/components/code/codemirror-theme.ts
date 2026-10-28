/**
 * The code viewer's theme, from the design tokens: every color is a token variable, so the
 * viewer follows the dark and light themes (and a generated one) with no second palette, and
 * syntax colors keep the contrast the token tests check (APCA Lc ≥ 60 on every surface).
 */
import { HighlightStyle, syntaxHighlighting, type TagStyle } from "@codemirror/language";
import { EditorView } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";

const token = (name: string) => `var(--${name})`;
// Tailwind inlines its theme variables, so the stacks are spelled out (as in globals.css).
const MONO =
  "var(--font-commit-mono), var(--font-geist-mono), ui-monospace, SFMono-Regular, monospace";
const SANS = "var(--font-geist-sans), ui-sans-serif, system-ui, sans-serif";

export const viewerTheme = EditorView.theme({
  "&": {
    height: "100%",
    color: token("text"),
    backgroundColor: token("bg"),
    fontSize: "12.5px",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": {
    fontFamily: MONO,
    lineHeight: "1.6",
    fontVariantLigatures: "none",
  },
  ".cm-content": { caretColor: token("text"), padding: "8px 0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: token("text") },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection":
    { backgroundColor: `color-mix(in oklch, ${token("layer-ui")} 28%, transparent)` },
  ".cm-gutters": {
    backgroundColor: token("bg"),
    color: token("text-faint"),
    borderRight: `1px solid ${token("line")}`,
  },
  ".cm-lineNumbers .cm-gutterElement": {
    padding: "0 12px 0 16px",
    minWidth: "40px",
    cursor: "pointer",
    fontVariantNumeric: "tabular-nums",
  },
  ".cm-lineNumbers .cm-gutterElement:hover": { color: token("text") },
  ".cm-foldGutter .cm-gutterElement": { padding: "0 4px", color: token("text-faint") },
  ".cm-activeLine": { backgroundColor: "transparent" },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: token("text-muted") },
  // The lines picked with ?L=: lit like a citation, the gutter marked too.
  ".cm-range-line": { backgroundColor: `color-mix(in oklch, ${token("warn")} 12%, transparent)` },
  ".cm-gutterElement.cm-range-gutter": {
    color: token("text"),
    backgroundColor: `color-mix(in oklch, ${token("warn")} 22%, transparent)`,
  },
  ".cm-foldPlaceholder": {
    backgroundColor: token("surface-3"),
    border: `1px solid ${token("line-strong")}`,
    color: token("text-muted"),
    borderRadius: "4px",
    padding: "0 4px",
  },
  ".cm-searchMatch": {
    backgroundColor: `color-mix(in oklch, ${token("warn")} 30%, transparent)`,
    outline: `1px solid color-mix(in oklch, ${token("warn")} 60%, transparent)`,
  },
  ".cm-searchMatch.cm-searchMatch-selected": {
    backgroundColor: `color-mix(in oklch, ${token("warn")} 55%, transparent)`,
  },
  ".cm-selectionMatch": {
    backgroundColor: `color-mix(in oklch, ${token("text")} 10%, transparent)`,
  },
  ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": {
    backgroundColor: token("surface-3"),
    outline: `1px solid ${token("line-strong")}`,
  },
  ".cm-panels": {
    backgroundColor: token("surface-1"),
    color: token("text"),
    fontFamily: SANS,
  },
  ".cm-panels.cm-panels-top": { borderBottom: `1px solid ${token("line")}` },
  ".cm-panel.cm-search": { padding: "6px 8px", fontSize: "13px" },
  ".cm-panel.cm-search input, .cm-panel.cm-search button": { fontSize: "13px" },
  ".cm-textfield": {
    backgroundColor: token("bg"),
    color: token("text"),
    border: `1px solid ${token("line-strong")}`,
    borderRadius: "6px",
    padding: "2px 6px",
  },
  ".cm-button": {
    backgroundImage: "none",
    backgroundColor: token("surface-2"),
    color: token("text"),
    border: `1px solid ${token("line-strong")}`,
    borderRadius: "6px",
    padding: "2px 8px",
  },
  ".cm-panel.cm-search label": { color: token("text-muted") },
  ".cm-panel button[name=close]": { color: token("text-muted") },
  ".cm-tooltip": {
    backgroundColor: token("surface-2"),
    color: token("text"),
    border: `1px solid ${token("line-strong")}`,
  },
  // A symbol's hover card draws its own box (symbol-hover-card.tsx).
  ".cm-tooltip.cm-tooltip-hover": {
    backgroundColor: "transparent",
    border: "none",
    zIndex: "40",
  },
  ".cm-symbol-hover": {
    textDecoration: `underline ${token("text-muted")}`,
    textUnderlineOffset: "3px",
    cursor: "pointer",
  },
});

/** Syntax colors from the tokens: one hue per kind of thing, comments quiet. Shared with the
 * code previews (lib/code/syntax.ts), so a preview reads like the viewer. */
export const SYNTAX_STYLES: readonly TagStyle[] = [
  { tag: [t.keyword, t.modifier, t.controlKeyword, t.operatorKeyword], color: token("layer-db") },
  { tag: [t.string, t.special(t.string), t.regexp, t.character], color: token("layer-svc") },
  { tag: [t.number, t.bool, t.null, t.atom, t.unit], color: token("layer-api") },
  {
    tag: [
      t.function(t.variableName),
      t.function(t.propertyName),
      t.definition(t.function(t.variableName)),
    ],
    color: token("layer-ui"),
  },
  { tag: [t.typeName, t.className, t.namespace, t.definition(t.typeName)], color: token("warn") },
  { tag: [t.tagName, t.angleBracket], color: token("layer-ui") },
  { tag: [t.attributeName, t.propertyName, t.labelName], color: token("text") },
  {
    tag: [t.comment, t.lineComment, t.blockComment, t.docComment],
    color: token("text-faint"),
    fontStyle: "italic",
  },
  { tag: [t.meta, t.processingInstruction, t.annotation], color: token("text-muted") },
  { tag: [t.operator, t.punctuation, t.separator, t.bracket], color: token("text-muted") },
  { tag: t.heading, color: token("text"), fontWeight: "600" },
  { tag: [t.link, t.url], color: token("layer-ui"), textDecoration: "underline" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: t.strong, fontWeight: "600" },
  { tag: t.strikethrough, textDecoration: "line-through" },
  { tag: [t.inserted], color: token("ok") },
  { tag: [t.deleted, t.invalid], color: token("danger") },
  { tag: [t.self, t.special(t.variableName)], color: token("layer-db") },
];

export const viewerHighlight = syntaxHighlighting(HighlightStyle.define(SYNTAX_STYLES));
