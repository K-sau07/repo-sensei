import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { toast } from "sonner";
import { expect, screen, userEvent } from "storybook/test";

import { Button } from "@/components/ui/button";
import { Toaster } from "@/components/ui/sonner";

function Toasts() {
  return (
    <div className="flex flex-wrap gap-2">
      <Toaster />
      <Button variant="outline" onClick={() => toast.success("Indexing finished")}>
        Success
      </Button>
      <Button variant="outline" onClick={() => toast.error("Couldn't reach GitHub")}>
        Error
      </Button>
      <Button variant="outline" onClick={() => toast.warning("Usage at 80% of the cap")}>
        Warning
      </Button>
      <Button variant="outline" onClick={() => toast("Invitation sent")}>
        Info
      </Button>
    </div>
  );
}

const meta = { title: "UI/Toast", component: Toasts } satisfies Meta<typeof Toasts>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Shown: Story = {
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("button", { name: "Success" }));
    await expect(await screen.findByText("Indexing finished")).toBeInTheDocument();
  },
};
