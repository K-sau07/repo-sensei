"use client";

/**
 * Whether the views count calls guessed by their names alone (a method called on something of
 * unknown type, matched to the one or few methods of that name). Off, only edges at least half
 * sure are counted: a cycle or a matrix cell made of guesses would be coupling the code may lack.
 */
import { Switch } from "@/components/ui/switch";

import { useGraphState } from "./graph-state";

export function GuessesSwitch({ onChange }: { onChange?: () => void }) {
  const [state, setState] = useGraphState();
  return (
    <span className="flex items-center gap-1.5 text-xs text-fg-muted">
      <Switch
        checked={state.guesses}
        onCheckedChange={(checked) => {
          onChange?.();
          void setState({ guesses: checked, cycle: null });
        }}
        aria-label="Count calls guessed by name"
      />
      <span aria-hidden title="Calls matched by their names alone, under half sure">
        Guesses
      </span>
    </span>
  );
}
