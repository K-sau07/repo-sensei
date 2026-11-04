"use client";

/** Every keyboard shortcut (10.7.1), the same list as the `?` sheet's, with a filter. */
import { useState } from "react";

import { ShortcutList } from "@/components/shell/shortcuts-sheet";
import { Input } from "@/components/ui/input";

export function ShortcutsView() {
  const [filter, setFilter] = useState("");
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-fg-muted">
        Sequences like “g h” mean: press g, then h. Press <kbd className="font-mono">?</kbd>{" "}
        anywhere to see these.
      </p>
      <Input
        aria-label="Find a shortcut"
        placeholder="Find a shortcut…"
        value={filter}
        className="max-w-sm"
        onChange={(event) => {
          setFilter(event.target.value);
        }}
      />
      <ShortcutList filter={filter} headingLevel={2} />
    </div>
  );
}
