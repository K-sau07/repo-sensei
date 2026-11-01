"use client";

/** The dependency level a view shows: services, packages, modules, files or symbols. */
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DEP_LEVELS, LEVEL_LABELS, type DepLevel } from "@/lib/api/deps";

export function LevelSelect({
  value,
  onChange,
  levels = DEP_LEVELS,
}: {
  value: DepLevel;
  onChange: (level: DepLevel) => void;
  levels?: readonly DepLevel[];
}) {
  return (
    <Select
      value={value}
      onValueChange={(next) => {
        if (next) onChange(next);
      }}
    >
      <SelectTrigger className="w-36" aria-label="Level">
        <SelectValue>{LEVEL_LABELS[value]}</SelectValue>
      </SelectTrigger>
      <SelectContent>
        {levels.map((l) => (
          <SelectItem key={l} value={l}>
            {LEVEL_LABELS[l]}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
