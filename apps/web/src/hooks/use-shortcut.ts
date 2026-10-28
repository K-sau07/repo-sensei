import { useHotkeys } from "react-hotkeys-hook";

import { matchesProducedKey, shortcut, toHotkey, type ShortcutId } from "@/lib/shortcut-registry";

interface UseShortcutOptions {
  enabled?: boolean;
  /** Fire even while typing in an input (only for modifier shortcuts like ⌘K). */
  inInputs?: boolean;
  /** Fire inside an open dialog or menu too (defaults to `inInputs`: ⌘K works everywhere). A
   * page-level action, like searching the open file, sets it false. */
  inOverlays?: boolean;
  /** Fire with the focus in the code viewer (a read-only contenteditable), for its keys. */
  inEditor?: boolean;
}

// Plain-key shortcuts belong to the page: not to an open dialog, menu or list someone is using.
const OVERLAY = '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]';

function insideOverlay(event: KeyboardEvent): boolean {
  return event.target instanceof Element && event.target.closest(OVERLAY) !== null;
}

/** A text box that takes typing (the code viewer's is read-only). */
function inWritableTextbox(event: KeyboardEvent): boolean {
  return (
    event.target instanceof Element &&
    event.target.getAttribute("role") === "textbox" &&
    event.target.getAttribute("aria-readonly") !== "true"
  );
}

/** Bind a registered shortcut. Plain-letter shortcuts never fire while the user is typing, or
 * inside a dialog or menu. */
export function useShortcut(
  id: ShortcutId,
  handler: () => void,
  {
    enabled = true,
    inInputs = false,
    inOverlays = inInputs,
    inEditor = false,
  }: UseShortcutOptions = {},
): void {
  const { keys, label } = shortcut(id);
  // "?" or "[" are matched by the character typed. Shift (needed to type "?") is allowed, but
  // not ⌘/Ctrl/Alt: ⌘[ is the browser's Back and must stay that.
  const byCharacter = matchesProducedKey(keys);
  useHotkeys(
    toHotkey(keys),
    (event) => {
      event.preventDefault();
      handler();
    },
    {
      enabled: (event) => {
        if (!enabled) return false;
        if (byCharacter && (event.metaKey || event.ctrlKey || event.altKey)) return false;
        if (inEditor && !inInputs && inWritableTextbox(event)) return false;
        return inOverlays || !insideOverlay(event);
      },
      // CodeMirror's content is a contenteditable with role="textbox", which the library takes
      // for a form field before it looks at contenteditable: the editor's keys let that role in.
      enableOnFormTags: inInputs ? true : inEditor ? ["textbox"] : false,
      enableOnContentEditable: inEditor,
      useKey: byCharacter,
      ignoreModifiers: byCharacter,
      description: label,
    },
    [handler],
  );
}
