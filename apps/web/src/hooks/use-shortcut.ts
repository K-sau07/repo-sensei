import { useHotkeys } from "react-hotkeys-hook";

import { matchesProducedKey, shortcut, toHotkey, type ShortcutId } from "@/lib/shortcut-registry";

interface UseShortcutOptions {
  enabled?: boolean;
  /** Fire even while typing in an input (only for modifier shortcuts like ⌘K). */
  inInputs?: boolean;
}

/** Bind a registered shortcut. Plain-letter shortcuts never fire while the user is typing. */
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
      enabled: byCharacter
        ? (event) => enabled && !event.metaKey && !event.ctrlKey && !event.altKey
        : enabled,
      enableOnFormTags: inInputs,
      useKey: byCharacter,
      ignoreModifiers: byCharacter,
      description: label,
    },
    [handler],
  );
}
