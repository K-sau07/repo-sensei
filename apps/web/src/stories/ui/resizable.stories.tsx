import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";

function Panes() {
  return (
    <ResizablePanelGroup
      orientation="horizontal"
      className="h-64 max-w-3xl rounded-xl border border-line"
    >
      <ResizablePanel defaultSize="30%" minSize="20%">
        <div className="p-4 text-sm text-fg-muted">File tree</div>
      </ResizablePanel>
      <ResizableHandle withHandle />
      <ResizablePanel defaultSize="70%">
        <div className="p-4 text-sm text-fg-muted">Code</div>
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}

const meta = { title: "UI/Resizable", component: Panes } satisfies Meta<typeof Panes>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Horizontal: Story = {};
