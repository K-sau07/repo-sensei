import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { AlignLeft, Network, Waypoints } from "lucide-react";
import { expect, userEvent } from "storybook/test";

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

function Navigation() {
  return (
    <div className="flex max-w-2xl flex-col gap-10">
      <Breadcrumb>
        <BreadcrumbList>
          <BreadcrumbItem>
            <BreadcrumbLink href="#acme">acme</BreadcrumbLink>
          </BreadcrumbItem>
          <BreadcrumbSeparator />
          <BreadcrumbItem>
            <BreadcrumbLink href="#langflow">langflow</BreadcrumbLink>
          </BreadcrumbItem>
          <BreadcrumbSeparator />
          <BreadcrumbItem>
            <BreadcrumbPage>Trace</BreadcrumbPage>
          </BreadcrumbItem>
        </BreadcrumbList>
      </Breadcrumb>

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="ask">Ask</TabsTrigger>
          <TabsTrigger value="trace">Trace</TabsTrigger>
          <TabsTrigger value="graph" disabled>
            Graph
          </TabsTrigger>
        </TabsList>
        <TabsContent value="overview" className="pt-3 text-sm text-fg-muted">
          Architecture map, feature maps and health.
        </TabsContent>
        <TabsContent value="ask" className="pt-3 text-sm text-fg-muted">
          Ask about this repository.
        </TabsContent>
        <TabsContent value="trace" className="pt-3 text-sm text-fg-muted">
          Follow a request from the UI to the database.
        </TabsContent>
      </Tabs>

      <ToggleGroup defaultValue={["graph"]} aria-label="View">
        <ToggleGroupItem value="graph" aria-label="Graph">
          <Network aria-hidden />
        </ToggleGroupItem>
        <ToggleGroupItem value="flow" aria-label="Flow">
          <Waypoints aria-hidden />
        </ToggleGroupItem>
        <ToggleGroupItem value="outline" aria-label="Outline">
          <AlignLeft aria-hidden />
        </ToggleGroupItem>
      </ToggleGroup>

      <Accordion defaultValue={["why"]}>
        <AccordionItem value="why">
          <AccordionTrigger>Why this answer?</AccordionTrigger>
          <AccordionContent>
            Six citations, all verified against the indexed commit.
          </AccordionContent>
        </AccordionItem>
        <AccordionItem value="missing">
          <AccordionTrigger>What might be missing?</AccordionTrigger>
          <AccordionContent>Dynamic routes registered at runtime are inferred.</AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

const meta = { title: "UI/Navigation", component: Navigation } satisfies Meta<typeof Navigation>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dark: Story = {
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("tab", { name: "Trace" }));
    await expect(canvas.getByText("Follow a request from the UI to the database.")).toBeVisible();
    await userEvent.click(canvas.getByRole("button", { name: "What might be missing?" }));
    await expect(canvas.getByRole("button", { name: "What might be missing?" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  },
};

export const Light: Story = { globals: { theme: "light" } };
