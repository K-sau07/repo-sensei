"use client";

import { useEffect, useState } from "react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// Smallest unit first, with how many of it make the next unit.
const UNITS: [Intl.RelativeTimeFormatUnit, number, number][] = [
  ["minute", 60, 60],
  ["hour", 3600, 24],
  ["day", 24 * 3600, 7],
  ["week", 7 * 24 * 3600, 30 / 7],
  ["month", 30 * 24 * 3600, 12],
  ["year", 365 * 24 * 3600, Number.POSITIVE_INFINITY],
];

/** "just now", "3 minutes ago", "in 2 days". */
export function formatRelative(date: Date, now: Date, locale = "en"): string {
  const seconds = Math.round((date.getTime() - now.getTime()) / 1000);
  if (Math.abs(seconds) < 45) return "just now";
  const format = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  for (const [unit, size, perNext] of UNITS) {
    // Round the magnitude (Math.round(-1.5) is -1, which would make 90s ago "1 minute ago"),
    // and move up a unit when rounding reaches it ("60 minutes ago" reads as "1 hour ago").
    const count = Math.round(Math.abs(seconds) / size);
    if (count < perNext) return format.format(Math.sign(seconds) * count, unit);
  }
  return "just now";
}

interface RelativeTimeProps {
  date: Date | string;
  /** Fixed "now" for stories and tests; the live clock otherwise (refreshed every minute). */
  now?: Date;
  className?: string;
}

export function RelativeTime({ date, now, className }: RelativeTimeProps) {
  const value = typeof date === "string" ? new Date(date) : date;
  const valid = !Number.isNaN(value.getTime());
  const [clock, setClock] = useState(() => now ?? new Date());

  useEffect(() => {
    if (now) return;
    const timer = setInterval(() => {
      setClock(new Date());
    }, 60_000);
    return () => {
      clearInterval(timer);
    };
  }, [now]);

  if (!valid) return <span className={className}>unknown time</span>;
  const absolute = value.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          // Focusable so keyboard users can open the tooltip with the exact time too.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          <time dateTime={value.toISOString()} className={className} tabIndex={0} />
        }
      >
        {/* Server and client clocks differ by a few seconds; the client value wins. */}
        <span suppressHydrationWarning>{formatRelative(value, now ?? clock)}</span>
      </TooltipTrigger>
      <TooltipContent>{absolute}</TooltipContent>
    </Tooltip>
  );
}
