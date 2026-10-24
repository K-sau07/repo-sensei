"use client";

/**
 * Keyboard moves over result lanes, through the shortcut registry: j/k within a lane, ←/→ to
 * the lane beside, ↵ to open, t to trace (not yet built), / to the search field. The results
 * are found in the DOM (`[data-lane]` holding `[data-card]` links), so lanes that page in more
 * results need no bookkeeping here.
 */
import { useState, type RefObject } from "react";
import { toast } from "sonner";

import { useShortcut } from "@/hooks/use-shortcut";

export const LANE_ATTR = "data-lane";
export const CARD_ATTR = "data-card";

const lanesIn = (root: HTMLElement) =>
  [...root.querySelectorAll<HTMLElement>(`[${LANE_ATTR}]`)].filter(
    (lane) => lane.querySelector(`[${CARD_ATTR}]`) !== null,
  );
const cardsIn = (lane: Element) => [...lane.querySelectorAll<HTMLElement>(`[${CARD_ATTR}]`)];

function focusedCard(root: HTMLElement): HTMLElement | null {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || !root.contains(active)) return null;
  return active.closest<HTMLElement>(`[${CARD_ATTR}]`);
}

export interface ResultKeysOptions {
  root: RefObject<HTMLElement | null>;
  /** The search field, for `/`. */
  input: RefObject<HTMLInputElement | null>;
  /** A lane is about to get focus (on a narrow screen only one lane shows: show it first). */
  onLane?: (lane: string) => void;
  enabled?: boolean;
}

/** Binds the keys; returns whether a result has focus (↵ and t only apply then). */
export function useResultKeys({ root, input, onLane, enabled = true }: ResultKeysOptions) {
  const [cardFocused, setCardFocused] = useState(false);

  const focus = (card: HTMLElement | undefined) => {
    if (!card) return;
    const lane = card.closest<HTMLElement>(`[${LANE_ATTR}]`)?.getAttribute(LANE_ATTR);
    if (lane && onLane) {
      onLane(lane);
      // A lane that was hidden shows on the next frame.
      requestAnimationFrame(() => {
        card.focus();
      });
    }
    card.focus();
  };

  const move = (step: 1 | -1) => {
    const container = root.current;
    if (!container) return;
    const current = focusedCard(container);
    const lane = current?.closest(`[${LANE_ATTR}]`) ?? lanesIn(container)[0];
    if (!lane) return;
    const cards = cardsIn(lane);
    const index = current ? cards.indexOf(current) : -1;
    const next = index === -1 ? (step === 1 ? 0 : cards.length - 1) : index + step;
    focus(cards[Math.max(0, Math.min(cards.length - 1, next))]);
  };

  const switchLane = (step: 1 | -1) => {
    const container = root.current;
    if (!container) return;
    const lanes = lanesIn(container);
    const current = focusedCard(container);
    const lane = current?.closest<HTMLElement>(`[${LANE_ATTR}]`) ?? null;
    const at = lane ? lanes.indexOf(lane) : -1;
    const target = lanes[at === -1 ? 0 : Math.max(0, Math.min(lanes.length - 1, at + step))];
    if (!target || target === lane) return;
    const index = current && lane ? cardsIn(lane).indexOf(current) : 0;
    const cards = cardsIn(target);
    focus(cards[Math.min(Math.max(0, index), cards.length - 1)]);
  };

  useShortcut(
    "list-next",
    () => {
      move(1);
    },
    { enabled },
  );
  useShortcut(
    "list-prev",
    () => {
      move(-1);
    },
    { enabled },
  );
  useShortcut(
    "lane-next",
    () => {
      switchLane(1);
    },
    { enabled },
  );
  useShortcut(
    "lane-prev",
    () => {
      switchLane(-1);
    },
    { enabled },
  );
  useShortcut(
    "focus-search",
    () => {
      input.current?.focus();
      input.current?.select();
    },
    { enabled },
  );
  // A link opens on ↵ by itself; going through the registry keeps the hint and the sheet right.
  useShortcut(
    "list-open",
    () => {
      const container = root.current;
      if (container) focusedCard(container)?.click();
    },
    { enabled: enabled && cardFocused },
  );
  useShortcut(
    "trace",
    () => {
      toast("Trace is coming soon.", { id: "trace-soon" });
    },
    { enabled: enabled && cardFocused },
  );

  return {
    /** Spread on the element holding the lanes. */
    containerProps: {
      onFocus: (event: React.FocusEvent) => {
        setCardFocused(
          event.target instanceof HTMLElement && event.target.closest(`[${CARD_ATTR}]`) !== null,
        );
      },
      onBlur: (event: React.FocusEvent) => {
        const next = event.relatedTarget;
        if (!(next instanceof HTMLElement) || !next.closest(`[${CARD_ATTR}]`))
          setCardFocused(false);
      },
    },
    /** Move from the search field into the results (↓). */
    enter: () => {
      move(1);
    },
  };
}
