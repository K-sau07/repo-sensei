"use client";

import { cn } from "cn";
import { Fragment } from "react";

import { Kbd, KbdGroup } from "@/components/ui/kbd";
import { usePlatform } from "@/hooks/use-platform";
import { describeShortcut, formatShortcut, type Platform } from "@/lib/shortcuts";

interface KeyHintProps {
  /** Platform-neutral shortcut, e.g. "mod+k" or "g r". */
  shortcut: string;
  /** Force a platform (stories, tests); detected from the browser otherwise. */
  platform?: Platform;
  className?: string;
}

/** Renders a keyboard shortcut with the right symbols for the user's platform. */
export function KeyHint({ shortcut, platform, className }: KeyHintProps) {
  const detected = usePlatform();
  const target = platform ?? detected;
  const steps = formatShortcut(shortcut, target);
  return (
    <KbdGroup
      className={cn("text-fg-muted", className)}
      aria-label={describeShortcut(shortcut, target)}
      role="img"
    >
      {steps.map((step, index) => (
        // Sequences are stable for a given shortcut, so the index is a fine key.

        <Fragment key={index}>
          {index > 0 && (
            <span aria-hidden className="px-0.5 text-xs">
              then
            </span>
          )}
          {step.map((key) => (
            <Kbd key={key.name} aria-hidden>
              {key.symbol}
            </Kbd>
          ))}
        </Fragment>
      ))}
    </KbdGroup>
  );
}
