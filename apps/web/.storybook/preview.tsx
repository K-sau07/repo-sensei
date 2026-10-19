import { withThemeByDataAttribute } from "@storybook/addon-themes";
import type { Preview } from "@storybook/nextjs-vite";

import { DEFAULT_THEME, MotionProvider, THEMES } from "../src/components/providers";
import { TooltipProvider } from "../src/components/ui/tooltip";
import { fontVariables } from "../src/fonts";

import "../src/app/globals.css";

// Like the app's root layout: fonts on <html>, so portalled content (tooltips, menus) gets them.
document.documentElement.classList.add(...fontVariables.split(" "));

const preview: Preview = {
  parameters: {
    layout: "fullscreen",
    backgrounds: { disable: true },
    controls: { expanded: true },
    // Accessibility violations fail the story's test (in the Storybook UI and in Vitest). The
    // whole body is checked so portalled content (dialogs, menus, tooltips) is covered too.
    a11y: { test: "error", context: "body" },
  },
  decorators: [
    (Story) => (
      <MotionProvider>
        <TooltipProvider>
          <main className="min-h-dvh bg-bg p-8 font-sans text-fg">
            <Story />
          </main>
        </TooltipProvider>
      </MotionProvider>
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
