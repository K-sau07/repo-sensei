import type { Meta, StoryObj } from "@storybook/nextjs-vite";

const STEPS = [1, 2, 3, 4, 6, 8, 12, 16];

function Spacing() {
  return (
    <div className="flex max-w-3xl flex-col gap-10">
      <section aria-labelledby="grid">
        <h2 id="grid" className="mb-4 text-sm font-medium text-fg-muted">
          4px grid
        </h2>
        <ul className="flex flex-col gap-2">
          {STEPS.map((step) => (
            <li key={step} className="flex items-center gap-4 text-sm tabular-nums">
              <span className="w-24 font-mono text-fg-muted">
                {step} · {step * 4}px
              </span>
              <span
                aria-hidden
                className="h-3 rounded-sm bg-layer-ui"
                style={{ width: step * 4 }}
              />
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="controls">
        <h2 id="controls" className="mb-4 text-sm font-medium text-fg-muted">
          Control heights and radii
        </h2>
        <div className="flex items-end gap-4">
          {(
            [
              ["h-control-sm rounded-sm", "28px · radius 6"],
              ["h-control-md rounded-sm", "32px · radius 6"],
              ["h-control-lg rounded-sm", "36px · radius 6"],
            ] as const
          ).map(([className, label]) => (
            <div
              key={label}
              className={`${className} flex items-center border border-line-strong bg-surface-2 px-3 text-sm`}
            >
              {label}
            </div>
          ))}
        </div>
      </section>

      <section aria-labelledby="elevation">
        <h2 id="elevation" className="mb-4 text-sm font-medium text-fg-muted">
          Surfaces and elevation (card radius 10, panel radius 14)
        </h2>
        <div className="grid grid-cols-3 gap-6">
          <div className="rounded-lg border border-line bg-surface-1 p-4 text-sm">
            surface-1 · flat
          </div>
          <div className="rounded-lg border border-line bg-surface-2 p-4 text-sm shadow-raised">
            surface-2 · raised
          </div>
          <div className="rounded-xl border border-line bg-surface-2 p-4 text-sm shadow-dialog">
            surface-2 · dialog
          </div>
        </div>
      </section>
    </div>
  );
}

const meta = {
  title: "Foundations/Spacing and depth",
  component: Spacing,
} satisfies Meta<typeof Spacing>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dark: Story = { globals: { theme: "dark" } };
export const Light: Story = { globals: { theme: "light" } };
