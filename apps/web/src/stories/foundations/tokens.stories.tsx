import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect } from "storybook/test";

import { apca, wcag } from "@/lib/theme/contrast";
import {
  COLOR_TOKENS,
  generateTheme,
  TEXT_CONTRAST_MINIMUMS,
  type ColorToken,
  type ThemeBase,
} from "@/lib/theme/generate";

const GROUPS: { title: string; tokens: ColorToken[] }[] = [
  {
    title: "Surfaces",
    tokens: ["bg", "surface-1", "surface-2", "surface-3", "line", "line-strong"],
  },
  { title: "Text", tokens: ["text", "text-muted", "text-faint"] },
  { title: "Signal and brand", tokens: ["signal", "brand", "brand-fg"] },
  { title: "Layers", tokens: ["layer-ui", "layer-api", "layer-svc", "layer-db"] },
  { title: "Status", tokens: ["danger", "warn", "ok"] },
];

function ColorTokens({ base }: { base: ThemeBase }) {
  const theme = generateTheme({ base });
  return (
    <div className="flex flex-col gap-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Color tokens</h1>
        <p className="text-fg-muted">
          Graphite Signal, {base} theme. Text tokens show APCA Lc and the WCAG 2 ratio on{" "}
          <code className="font-mono">--bg</code>.
        </p>
      </header>
      {GROUPS.map((group) => (
        <section key={group.title} aria-labelledby={`group-${group.title}`}>
          <h2 id={`group-${group.title}`} className="mb-3 text-sm font-medium text-fg-muted">
            {group.title}
          </h2>
          <ul className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
            {group.tokens.map((token) => {
              const minimum = TEXT_CONTRAST_MINIMUMS[token];
              return (
                <li
                  key={token}
                  className="flex items-center gap-3 rounded-lg border border-line bg-surface-1 p-3"
                >
                  <span
                    aria-hidden
                    className="size-10 shrink-0 rounded-md border border-line-strong"
                    style={{ background: `var(--${token})` }}
                  />
                  <span className="flex min-w-0 flex-col text-sm">
                    <span className="font-mono">--{token}</span>
                    <span className="truncate font-mono text-xs text-fg-muted">{theme[token]}</span>
                    {minimum !== undefined && (
                      <span className="text-xs text-fg-muted tabular-nums">
                        Lc {apca(theme[token], theme.bg).toFixed(0)} (min {minimum}) ·{" "}
                        {wcag(theme[token], theme.bg).toFixed(1)}:1
                      </span>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}

const meta = {
  title: "Foundations/Color tokens",
  component: ColorTokens,
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof ColorTokens>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dark: Story = {
  args: { base: "dark" },
  globals: { theme: "dark" },
  play: async ({ canvas }) => {
    const items = await canvas.findAllByRole("listitem");
    await expect(items).toHaveLength(COLOR_TOKENS.length);
    // Guards the accessibility checks: they are only meaningful with Tailwind and tokens loaded.
    await expect(getComputedStyle(items[0] as Element).borderRadius).toBe("10px");
    await expect(getComputedStyle(document.documentElement).fontSize).toBe("16px");
    await expectThemeApplied("dark");
  },
};

export const Light: Story = {
  args: { base: "light" },
  globals: { theme: "light" },
  play: async () => {
    await expectThemeApplied("light");
  },
};

async function expectThemeApplied(base: ThemeBase) {
  const root = document.documentElement;
  await expect(root.dataset.theme).toBe(base);
  await expect(getComputedStyle(root).getPropertyValue("--bg").trim()).toBe(
    generateTheme({ base }).bg,
  );
}
