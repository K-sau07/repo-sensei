"use client";

import { domAnimation, LazyMotion, MotionConfig } from "motion/react";
import { ThemeProvider } from "next-themes";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import { type ReactNode } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryProvider } from "@/lib/query/provider";
import { MockGate } from "@/mocks/mock-gate";

export const THEMES = ["dark", "light"] as const;
export const DEFAULT_THEME = "dark";

/** Motion setup: lazy-loaded DOM features and the user's reduced-motion preference. */
export function MotionProvider({ children }: { children: ReactNode }) {
  return (
    // `strict` makes a full `motion.*` import an error, keeping the bundle on `m.*`.
    <LazyMotion features={domAnimation} strict>
      <MotionConfig reducedMotion="user">{children}</MotionConfig>
    </LazyMotion>
  );
}

/** App-wide client providers: theme (data-theme on <html>), URL state, server state and motion. */
export function Providers({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider
      attribute="data-theme"
      themes={[...THEMES]}
      defaultTheme={DEFAULT_THEME}
      enableSystem
      disableTransitionOnChange
    >
      <NuqsAdapter>
        <MockGate>
          <QueryProvider>
            <MotionProvider>
              <TooltipProvider>{children}</TooltipProvider>
            </MotionProvider>
          </QueryProvider>
        </MockGate>
      </NuqsAdapter>
    </ThemeProvider>
  );
}
