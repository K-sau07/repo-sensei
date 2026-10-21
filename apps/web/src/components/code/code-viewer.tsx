"use client";

/**
 * The read-only code viewer (CodeMirror 6). Loaded only in the browser (see `file-view.tsx`), and
 * each language's grammar only when a file needs it. Lines picked in the gutter (click, then
 * Shift+click to extend) become the URL's `?L=`; a range arriving from the URL (a deep link, Back
 * or Forward) is lit and scrolled to.
 */
import { defaultKeymap } from "@codemirror/commands";
import {
  bracketMatching,
  codeFolding,
  foldGutter,
  foldKeymap,
  LanguageDescription,
} from "@codemirror/language";
import {
  highlightSelectionMatches,
  openSearchPanel,
  search,
  searchKeymap,
} from "@codemirror/search";
import {
  Compartment,
  EditorState,
  RangeSet,
  StateEffect,
  StateField,
  type Extension,
} from "@codemirror/state";
import {
  Decoration,
  drawSelection,
  EditorView,
  GutterMarker,
  gutterLineClass,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
  type DecorationSet,
} from "@codemirror/view";
import { useEffect, useImperativeHandle, useRef, type Ref } from "react";

import { clampLines, extendLines, sameLines, type LineRange } from "@/lib/code/lines";

import { viewerHighlight, viewerTheme } from "./codemirror-theme";
import { FILE_RECEIVED_MARK } from "./use-code";

/** Measured from the file's arrival to the first frame showing it (the budget is 300 ms). */
export const PAINT_MEASURE = "code-viewer:paint";

export interface CodeViewerHandle {
  /** Open the search panel (⌘F inside the viewer does the same). */
  openSearch: () => void;
}

export interface CodeViewerProps {
  path: string;
  /** The API's language id; the file name is tried first. */
  language: string | null;
  content: string;
  lines: LineRange | null;
  onLinesChange: (lines: LineRange) => void;
  /** The lines the text selection spans, or null when nothing is selected. */
  onSelectionLines?: (lines: LineRange | null) => void;
  wrap: boolean;
  handle?: Ref<CodeViewerHandle>;
}

const setRange = StateEffect.define<LineRange | null>();

const rangeField = StateField.define<LineRange | null>({
  create: () => null,
  update: (value, transaction) => {
    for (const effect of transaction.effects) if (effect.is(setRange)) return effect.value;
    return value;
  },
});

const rangeLine = Decoration.line({ class: "cm-range-line" });
const rangeGutter = new (class extends GutterMarker {
  override elementClass = "cm-range-gutter";
})();

/** The picked lines, within the document. */
function visibleRange(state: EditorState): LineRange | null {
  const range = state.field(rangeField);
  return range ? clampLines(range, state.doc.lines) : null;
}

const rangeDecorations = EditorView.decorations.compute([rangeField], (state): DecorationSet => {
  const range = visibleRange(state);
  if (!range) return Decoration.none;
  const marks = [];
  for (let line = range.start; line <= range.end; line += 1) {
    marks.push(rangeLine.range(state.doc.line(line).from));
  }
  return Decoration.set(marks);
});

const noMarks = RangeSet.of<GutterMarker>([]);

const rangeGutterMarks = gutterLineClass.compute([rangeField], (state) => {
  const range = visibleRange(state);
  if (!range) return noMarks;
  const marks = [];
  for (let line = range.start; line <= range.end; line += 1) {
    marks.push(rangeGutter.range(state.doc.line(line).from));
  }
  return RangeSet.of<GutterMarker>(marks);
});

function scrollTo(view: EditorView, range: LineRange) {
  const shown = clampLines(range, view.state.doc.lines);
  if (!shown) return;
  // A short range is centred; a long one starts at the top so its first line is in view.
  const y = shown.end - shown.start < 20 ? "center" : "start";
  view.dispatch({
    effects: EditorView.scrollIntoView(view.state.doc.line(shown.start).from, { y, yMargin: 48 }),
  });
}

function selectionLines(state: EditorState): LineRange | null {
  const { main } = state.selection;
  if (main.empty) return null;
  const start = state.doc.lineAt(main.from).number;
  // A selection ending at the start of a line doesn't include that line.
  const endLine = state.doc.lineAt(main.to);
  const end =
    main.to === endLine.from && endLine.number > start ? endLine.number - 1 : endLine.number;
  return { start, end };
}

async function loadLanguage(path: string, language: string | null): Promise<Extension | null> {
  const { languages } = await import("@codemirror/language-data");
  const name = path.slice(path.lastIndexOf("/") + 1);
  const found =
    LanguageDescription.matchFilename(languages, name) ??
    (language ? LanguageDescription.matchLanguageName(languages, language, false) : null);
  return found ? found.load() : null;
}

/** When the file arrived (its mark, taken once), or now if it came from the cache. */
function takeArrival(): number {
  const mark = performance.getEntriesByName(FILE_RECEIVED_MARK, "mark").at(-1);
  performance.clearMarks(FILE_RECEIVED_MARK);
  return mark?.startTime ?? performance.now();
}

export function CodeViewer({
  path,
  language,
  content,
  lines,
  onLinesChange,
  onSelectionLines,
  wrap,
  handle,
}: CodeViewerProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const languageSlot = useRef(new Compartment());
  const wrapSlot = useRef(new Compartment());
  // The latest props for CodeMirror's handlers, which are created once per document.
  const latest = useRef({ lines, onLinesChange, onSelectionLines, wrap });
  // The range this viewer last put in the URL: when it comes back as a prop, don't scroll.
  const emitted = useRef<LineRange | null>(null);
  // When this file arrived; kept across a remount of the editor (Strict Mode mounts twice).
  const arrival = useRef<number | null>(null);

  useEffect(() => {
    latest.current = { lines, onLinesChange, onSelectionLines, wrap };
  });

  useImperativeHandle(handle, () => ({
    openSearch: () => {
      if (view.current) openSearchPanel(view.current);
    },
  }));

  useEffect(() => {
    const parent = host.current;
    if (!parent) return;
    const start = (arrival.current ??= takeArrival());
    const initial = latest.current;
    const gutterClick = (editor: EditorView, line: number, extend: boolean) => {
      const next = extendLines(editor.state.field(rangeField), line, extend);
      emitted.current = next;
      editor.dispatch({ effects: setRange.of(next) });
      latest.current.onLinesChange(next);
    };
    const editor = new EditorView({
      parent,
      state: EditorState.create({
        doc: content,
        extensions: [
          EditorState.readOnly.of(true),
          EditorView.contentAttributes.of({
            "aria-label": `Contents of ${path}`,
            "aria-readonly": "true",
            // Focusable however axe reads contenteditable, so the scrolling text is reachable.
            tabindex: "0",
          }),
          lineNumbers({
            domEventHandlers: {
              mousedown: (editor, block, event) => {
                if (!(event instanceof MouseEvent) || event.button !== 0) return false;
                event.preventDefault();
                gutterClick(editor, editor.state.doc.lineAt(block.from).number, event.shiftKey);
                return true;
              },
            },
          }),
          highlightActiveLineGutter(),
          foldGutter(),
          codeFolding(),
          highlightSpecialChars(),
          drawSelection(),
          bracketMatching(),
          highlightSelectionMatches(),
          search({ top: true }),
          keymap.of([...searchKeymap, ...foldKeymap, ...defaultKeymap]),
          rangeField.init(() => initial.lines),
          rangeDecorations,
          rangeGutterMarks,
          languageSlot.current.of([]),
          wrapSlot.current.of(initial.wrap ? EditorView.lineWrapping : []),
          viewerTheme,
          viewerHighlight,
          EditorView.updateListener.of((update) => {
            if (update.selectionSet)
              latest.current.onSelectionLines?.(selectionLines(update.state));
          }),
        ],
      }),
    });
    view.current = editor;
    emitted.current = initial.lines;
    // How long the file took to show: from its arrival to just after the first frame that
    // paints it (a task queued from requestAnimationFrame runs once that frame is drawn).
    let painted: ReturnType<typeof setTimeout> | undefined;
    const frame = requestAnimationFrame(() => {
      painted = setTimeout(() => {
        performance.measure(PAINT_MEASURE, { start });
      }, 0);
    });
    if (initial.lines) scrollTo(editor, initial.lines);

    let cancelled = false;
    const slot = languageSlot.current;
    loadLanguage(path, language).then(
      (support) => {
        if (!cancelled && support) editor.dispatch({ effects: slot.reconfigure(support) });
      },
      () => {
        // No grammar (offline, or a chunk failed): the file stays readable as plain text.
      },
    );
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
      clearTimeout(painted);
      editor.destroy();
      view.current = null;
    };
  }, [path, language, content]);

  // A range from the URL (a deep link, Back, Forward): light it and scroll to it.
  useEffect(() => {
    const editor = view.current;
    if (!editor || sameLines(editor.state.field(rangeField), lines)) return;
    editor.dispatch({ effects: setRange.of(lines) });
    if (lines && !sameLines(lines, emitted.current)) scrollTo(editor, lines);
    emitted.current = lines;
  }, [lines]);

  useEffect(() => {
    view.current?.dispatch({
      effects: wrapSlot.current.reconfigure(wrap ? EditorView.lineWrapping : []),
    });
  }, [wrap]);

  return <div ref={host} className="h-full min-h-0" data-testid="code-viewer" />;
}
