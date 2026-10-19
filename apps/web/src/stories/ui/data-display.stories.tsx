import type { Meta, StoryObj } from "@storybook/nextjs-vite";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Kbd, KbdGroup } from "@/components/ui/kbd";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const BADGES = ["default", "secondary", "outline", "ghost", "destructive"] as const;
const AVATAR_PIXEL =
  "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1 1'><rect width='1' height='1' fill='%233EE0B5'/></svg>";

function DataDisplay() {
  return (
    <div className="flex max-w-3xl flex-col gap-10">
      <div className="flex flex-wrap items-center gap-2">
        {BADGES.map((variant) => (
          <Badge key={variant} variant={variant}>
            {variant}
          </Badge>
        ))}
      </div>

      <div className="flex items-center gap-3">
        <Avatar>
          <AvatarImage src={AVATAR_PIXEL} alt="octo" />
          <AvatarFallback>OC</AvatarFallback>
        </Avatar>
        <Avatar>
          <AvatarFallback>SK</AvatarFallback>
        </Avatar>
        <Separator orientation="vertical" className="h-6" />
        <KbdGroup>
          <Kbd>⌘</Kbd>
          <Kbd>K</Kbd>
        </KbdGroup>
      </div>

      <div className="flex flex-col gap-3">
        {[0, 40, 100].map((value) => (
          <Progress key={value} value={value} aria-label={`Indexing ${value}%`} />
        ))}
      </div>

      <div className="flex items-center gap-3" role="status" aria-label="Loading repositories">
        <Skeleton className="size-10 rounded-full" />
        <div className="flex flex-1 flex-col gap-2">
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-4 w-1/3" />
        </div>
      </div>

      <ScrollArea className="h-32 rounded-lg border border-line">
        <ul className="p-3 text-sm">
          {Array.from({ length: 20 }, (_, i) => (
            <li key={i} className="py-1 font-mono text-fg-muted">
              src/routes/checkout_{i}.py
            </li>
          ))}
        </ul>
      </ScrollArea>

      <Table>
        <TableCaption>Recent index jobs</TableCaption>
        <TableHeader>
          <TableRow>
            <TableHead>Commit</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Files</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {[
            ["a1b2c3d", "Done", "11,482"],
            ["e4f5a6b", "Failed", "0"],
          ].map(([sha, status, files]) => (
            <TableRow key={sha}>
              <TableCell className="font-mono">{sha}</TableCell>
              <TableCell>{status}</TableCell>
              <TableCell className="text-right tabular-nums">{files}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

const meta = { title: "UI/Data display", component: DataDisplay } satisfies Meta<
  typeof DataDisplay
>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dark: Story = {};
export const Light: Story = { globals: { theme: "light" } };
