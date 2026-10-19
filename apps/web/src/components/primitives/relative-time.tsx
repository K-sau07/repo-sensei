"use client";

import { useEffect, useState } from "react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** "just now", "3 minutes ago", "in 2 days". */
export function formatRelative(date: Date, now: Date, locale = "en"): string {
  const seconds = Math.round((date.getTime() - now.getTime()) / 1000);
  if (Math.abs(seconds) < 45) return "just now";
  const format = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size || unit === "minute") {
      // Round the magnitude (Math.round(-1.5) is -1, which would make 90s ago "1 minute ago").
      return format.format(Math.sign(seconds) * Math.round(Math.abs(seconds) / size), unit);
    }
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
