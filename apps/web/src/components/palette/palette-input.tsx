"use client";

import { SearchIcon } from "lucide-react";
import { useState, useSyncExternalStore, type ComponentProps, type RefObject } from "react";

import { InputGroup, InputGroupAddon } from "@/components/ui/input-group";

interface ListState {
  listId: string | undefined;
  activeId: string | undefined;
}

/** A store over the list's DOM, for `useSyncExternalStore`. */
export function listStore(list: RefObject<HTMLElement | null>) {
  return {
    subscribe: (changed: () => void) => {
      const element = list.current;
      if (!element) return () => undefined;
      const observer = new MutationObserver(changed);
      observer.observe(element, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeFilter: ["aria-selected", "id"],
      });
      return () => {
        observer.disconnect();
      };
    },
    // A string, so an unchanged state is the same snapshot.
    read: () => {
      const element = list.current;
      const active = element?.querySelector('[cmdk-item][aria-selected="true"]');
      return `${element?.id ?? ""} ${active?.id ?? ""}`;
    },
  };
}

/**
 * The listbox's id and its selected option's, read from the DOM cmdk renders. (cmdk's own input
 * only learns the selected option when cmdk moves the selection itself, not when the palette
 * picks it, so `aria-activedescendant` would go stale.)
 */
export function useListState(list: RefObject<HTMLElement | null>): ListState {
  // Made once: the same subscribe for as long as the palette is open, so its one observer isn't
  // rebuilt on every keystroke.
  const [store] = useState(() => listStore(list));
  const [listId, activeId] = useSyncExternalStore(store.subscribe, store.read, () => " ").split(
    " ",
  );
  const present = (id: string | undefined) => (id === "" ? undefined : id);
  return { listId: present(listId), activeId: present(activeId) };
}

interface PaletteInputProps extends Omit<ComponentProps<"input">, "value" | "onChange"> {
  value: string;
  onValueChange: (value: string) => void;
  listId: string | undefined;
  activeId: string | undefined;
}

/** The palette's combobox: looks like `CommandInput`, but its ARIA state is the palette's. */
export function PaletteInput({
  value,
  onValueChange,
  listId,
  activeId,
  ...props
}: PaletteInputProps) {
  return (
    <div data-slot="command-input-wrapper" className="p-1 pb-0">
      <InputGroup className="h-8! rounded-sm! border-input/30 bg-input/30 shadow-none! *:data-[slot=input-group-addon]:pl-2!">
        <input
          {...props}
          data-slot="command-input"
          role="combobox"
          aria-expanded="true"
          aria-autocomplete="list"
          aria-controls={listId}
          aria-activedescendant={activeId}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          type="text"
          value={value}
          onChange={(event) => {
            onValueChange(event.target.value);
          }}
          className="w-full bg-transparent text-sm outline-hidden"
        />
        <InputGroupAddon>
          <SearchIcon className="size-4 shrink-0 opacity-50" />
        </InputGroupAddon>
      </InputGroup>
    </div>
  );
}
