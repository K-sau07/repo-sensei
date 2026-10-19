import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { ArrowRight, Plus, Trash2 } from "lucide-react";
import { expect, userEvent } from "storybook/test";

import { Button } from "@/components/ui/button";

const VARIANTS = ["default", "secondary", "outline", "ghost", "destructive", "link"] as const;
const SIZES = ["xs", "sm", "default", "lg"] as const;

function ButtonGallery() {
  return (
    <div className="flex flex-col gap-6">
      {VARIANTS.map((variant) => (
        <div key={variant} className="flex flex-wrap items-center gap-3">
          <span className="w-24 text-sm text-fg-muted">{variant}</span>
          {SIZES.map((size) => (
            <Button key={size} variant={variant} size={size}>
              {size}
            </Button>
          ))}
          <Button variant={variant} disabled>
            Disabled
          </Button>
        </div>
      ))}
      <div className="flex flex-wrap items-center gap-3">
        <span className="w-24 text-sm text-fg-muted">icons</span>
        <Button>
          <Plus data-icon="inline-start" aria-hidden />
          Connect repo
        </Button>
        <Button variant="outline">
          Continue
          <ArrowRight data-icon="inline-end" aria-hidden />
        </Button>
        <Button variant="ghost" size="icon" aria-label="Add">
          <Plus aria-hidden />
        </Button>
        <Button variant="destructive" size="icon-sm" aria-label="Delete">
          <Trash2 aria-hidden />
        </Button>
      </div>
    </div>
  );
}

const meta = {
  title: "UI/Button",
  component: ButtonGallery,
} satisfies Meta<typeof ButtonGallery>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dark: Story = {
  play: async ({ canvas }) => {
    const button = canvas.getByRole("button", { name: "Connect repo" });
    await expect(getComputedStyle(button).height).toBe("32px");
    await expect(getComputedStyle(button).borderRadius).toBe("6px");
    await userEvent.tab();
    await expect(canvas.getAllByRole("button")[0]).toHaveFocus();
  },
};

export const Light: Story = { globals: { theme: "light" } };
