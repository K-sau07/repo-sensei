import type { Meta, StoryObj } from "@storybook/nextjs-vite";
import { expect, screen, userEvent } from "storybook/test";

import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

const ROLES = [
  { value: "viewer", label: "Viewer" },
  { value: "member", label: "Member" },
  { value: "admin", label: "Admin" },
];

function FormControls() {
  return (
    <div className="grid max-w-xl gap-6">
      <div className="grid gap-2">
        <Label htmlFor="name">Organization name</Label>
        <Input id="name" placeholder="Acme Inc" />
      </div>
      <div className="grid gap-2">
        <Label htmlFor="slug">URL</Label>
        <Input id="slug" defaultValue="api" aria-invalid aria-describedby="slug-error" />
        <p id="slug-error" className="text-sm text-danger">
          That name is reserved.
        </p>
      </div>
      <div className="grid gap-2">
        <Label htmlFor="disabled">Disabled</Label>
        <Input id="disabled" disabled defaultValue="read only" />
      </div>
      <div className="grid gap-2">
        <Label htmlFor="question">Question</Label>
        <Textarea id="question" placeholder="Where does checkout call the database?" />
      </div>
      <div className="grid gap-2">
        <Label id="role-label">Role</Label>
        <Select items={ROLES} defaultValue="member">
          <SelectTrigger aria-labelledby="role-label" className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ROLES.map((role) => (
              <SelectItem key={role.value} value={role.value}>
                {role.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-wrap gap-6">
        <Label>
          <Checkbox /> Unchecked
        </Label>
        <Label>
          <Checkbox defaultChecked /> Checked
        </Label>
        <Label>
          <Checkbox indeterminate /> Some
        </Label>
        <Label>
          <Checkbox disabled /> Disabled
        </Label>
      </div>
      <div className="flex flex-wrap gap-6">
        <Label>
          <Switch /> Off
        </Label>
        <Label>
          <Switch defaultChecked /> On
        </Label>
        <Label>
          <Switch disabled /> Disabled
        </Label>
      </div>
      <RadioGroup defaultValue="comfortable" aria-label="Density" className="flex gap-6">
        <Label>
          <RadioGroupItem value="comfortable" /> Comfortable
        </Label>
        <Label>
          <RadioGroupItem value="compact" /> Compact
        </Label>
      </RadioGroup>
    </div>
  );
}

const meta = {
  title: "UI/Form controls",
  component: FormControls,
} satisfies Meta<typeof FormControls>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Dark: Story = {
  play: async ({ canvas }) => {
    const input = canvas.getByLabelText("Organization name");
    await userEvent.type(input, "Acme");
    await expect(input).toHaveValue("Acme");
    await expect(getComputedStyle(input).height).toBe("32px");
  },
};

export const Light: Story = { globals: { theme: "light" } };

export const SelectOpen: Story = {
  play: async ({ canvas }) => {
    await userEvent.click(canvas.getByRole("combobox", { name: "Role" }));
    await expect(await screen.findByRole("listbox")).toBeVisible();
  },
};
