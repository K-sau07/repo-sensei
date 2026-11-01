"use client";

/** What the consent and device pages share: the card, the scopes asked for, and which org the
 * access is for. */
import { Check } from "lucide-react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { scopeLabel } from "@/lib/api/oauth";

export function GrantCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex w-full max-w-md flex-col gap-5 rounded-xl border border-line bg-surface-1 p-8 shadow-dialog">
      {children}
    </div>
  );
}

export function ScopeList({ scopes }: { scopes: readonly string[] }) {
  return (
    <ul aria-label="What it can do" className="flex flex-col gap-2">
      {scopes.map((scope) => {
        const label = scopeLabel(scope);
        return (
          <li key={scope} className="flex gap-2 text-sm">
            <Check aria-hidden className="mt-0.5 size-4 shrink-0 text-ok" />
            <span>
              <span className="font-medium">{label.title}</span>
              {label.detail ? <span className="block text-fg-muted">{label.detail}</span> : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export interface OrgChoice {
  slug: string;
  name: string;
}

/** The org the access is for: a choice when there are several. */
export function OrgPicker({
  orgs,
  value,
  onChange,
}: {
  orgs: readonly OrgChoice[];
  value: string;
  onChange: (slug: string) => void;
}) {
  if (orgs.length === 1) {
    return (
      <p className="text-sm text-fg-muted">
        For <span className="font-medium text-fg">{orgs[0]?.name}</span>
      </p>
    );
  }
  return (
    <div className="flex items-center gap-2 text-sm text-fg-muted">
      <span aria-hidden>For</span>
      <Select
        value={value}
        onValueChange={(next) => {
          if (next) onChange(next);
        }}
      >
        <SelectTrigger className="w-56" aria-label="Organization">
          <SelectValue>{orgs.find((o) => o.slug === value)?.name}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          {orgs.map((org) => (
            <SelectItem key={org.slug} value={org.slug}>
              {org.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/** The org to start on: the one last used, else the first. */
export function firstOrg(orgs: readonly OrgChoice[], last: string | null | undefined): string {
  return orgs.find((o) => o.slug === last)?.slug ?? orgs[0]?.slug ?? "";
}
