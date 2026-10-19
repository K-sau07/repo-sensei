import { withThemeByDataAttribute } from "@storybook/addon-themes";
import type { Preview } from "@storybook/nextjs-vite";

import { DEFAULT_THEME, MotionProvider, THEMES } from "../src/components/providers";
import { fontVariables } from "../src/fonts";

import "../src/app/globals.css";

const preview: Preview = {
  parameters: {
    layout: "fullscreen",
    backgrounds: { disable: true },
    controls: { expanded: true },
    // Accessibility violations fail the story's test (in the Storybook UI and in Vitest).
    a11y: { test: "error" },
  },
  decorators: [
    (Story) => (
      <MotionProvider>
        <div className={`${fontVariables} min-h-dvh bg-bg p-8 font-sans text-fg`}>
          <Story />
        </div>
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
