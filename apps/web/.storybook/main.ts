import type { StorybookConfig } from "@storybook/nextjs-vite";
import tailwindcss from "@tailwindcss/vite";

function toArray(value: string | string[] | undefined): string[] {
  if (value === undefined) return [];
  return Array.isArray(value) ? value : [value];
}

const config: StorybookConfig = {
  stories: ["../src/**/*.stories.@(ts|tsx)"],
  addons: [
    "@storybook/addon-docs",
    "@storybook/addon-a11y",
    "@storybook/addon-themes",
    "@storybook/addon-vitest",
    "msw-storybook-addon",
  ],
  // Serves mockServiceWorker.js for the MSW addon.
  staticDirs: ["../public"],
  framework: { name: "@storybook/nextjs-vite", options: {} },
  core: { disableTelemetry: true },
  // Next compiles Tailwind through its Turbopack loader; Storybook runs on Vite and needs the plugin.
  viteFinal: (viteConfig) => ({
    ...viteConfig,
    plugins: [...(viteConfig.plugins ?? []), tailwindcss()],
    optimizeDeps: {
      ...viteConfig.optimizeDeps,
      // Crawl every source file up front so Vite never finds a new dependency mid-run and
      // reloads (which fails the run on a cold cache, e.g. in CI).
      entries: [
        ...toArray(viteConfig.optimizeDeps?.entries),
        ".storybook/preview.tsx",
        "src/**/*.{ts,tsx}",
      ],
    },
  }),
};

export default config;
