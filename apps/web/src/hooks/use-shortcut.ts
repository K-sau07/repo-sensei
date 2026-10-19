import { useHotkeys } from "react-hotkeys-hook";

import { matchesProducedKey, shortcut, toHotkey, type ShortcutId } from "@/lib/shortcut-registry";

interface UseShortcutOptions {
  enabled?: boolean;
  /** Fire even while typing in an input (only for modifier shortcuts like ⌘K). */
  inInputs?: boolean;
}

// Plain-key shortcuts belong to the page: not to an open dialog, menu or list someone is using.
const OVERLAY = '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]';

function insideOverlay(event: KeyboardEvent): boolean {
  return event.target instanceof Element && event.target.closest(OVERLAY) !== null;
}

/** Bind a registered shortcut. Plain-letter shortcuts never fire while the user is typing, or
 * inside a dialog or menu. */
export function useShortcut(
  id: ShortcutId,
  handler: () => void,
  { enabled = true, inInputs = false }: UseShortcutOptions = {},
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
        return inInputs || !insideOverlay(event);
      },
      enableOnFormTags: inInputs,
      useKey: byCharacter,
      ignoreModifiers: byCharacter,
      description: label,
    },
    [handler],
  );
}
