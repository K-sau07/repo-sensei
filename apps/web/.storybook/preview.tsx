import { withThemeByDataAttribute } from "@storybook/addon-themes";
import type { Preview } from "@storybook/nextjs-vite";
import { QueryClientProvider } from "@tanstack/react-query";
import { mswLoader } from "msw-storybook-addon/csf3";
import { NuqsTestingAdapter } from "nuqs/adapters/testing";

import { DEFAULT_THEME, MotionProvider, THEMES } from "../src/components/providers";
import { TooltipProvider } from "../src/components/ui/tooltip";
import { fontVariables } from "../src/fonts";
import { makeQueryClient } from "../src/lib/query/provider";
import { handlers } from "../src/mocks/handlers";

import "../src/app/globals.css";

// Like the app's root layout: fonts on <html>, so portalled content (tooltips, menus) gets them.
document.documentElement.classList.add(...fontVariables.split(" "));

const preview: Preview = {
  // Stories talk to the mock API; a story can override with `parameters.msw`.
  loaders: [mswLoader()],
  parameters: {
    msw: handlers,
    layout: "fullscreen",
    backgrounds: { disable: true },
    controls: { expanded: true },
    // Accessibility violations fail the story's test (in the Storybook UI and in Vitest). The
    // whole body is checked so portalled content (dialogs, menus, tooltips) is covered too.
    a11y: { test: "error", context: "body" },
  },
  decorators: [
    (Story, context) => (
      // A fresh query cache per story, so stories never share data.
      <QueryClientProvider client={makeQueryClient()}>
        {/* URL state (filters, tabs) without a router: kept in memory per story. */}
        <NuqsTestingAdapter>
          <MotionProvider>
            <TooltipProvider>
              {/* `bare` stories (like the app shell) bring their own <main> and layout. */}
              {context.parameters.bare === true ? (
                <div className="min-h-dvh bg-bg font-sans text-fg">
                  <Story />
                </div>
              ) : (
                <main className="min-h-dvh bg-bg p-8 font-sans text-fg">
                  <Story />
                </main>
              )}
            </TooltipProvider>
          </MotionProvider>
        </NuqsTestingAdapter>
      </QueryClientProvider>
    ),
    withThemeByDataAttribute({
      themes: Object.fromEntries(THEMES.map((theme) => [theme, theme])),
      defaultTheme: DEFAULT_THEME,
      attributeName: "data-theme",
      parentSelector: "html",
    }),
  ],
};

export default preview;
