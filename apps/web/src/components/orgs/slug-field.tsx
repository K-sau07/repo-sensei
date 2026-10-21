"use client";

import { Check, Loader2, X } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { $api } from "@/lib/api/client";
import { SLUG_MESSAGES, slugProblem } from "@/lib/orgs/slug";

export type SlugStatus = "idle" | "checking" | "available" | "unavailable";

interface SlugFieldProps {
  id: string;
  value: string;
  onChange: (value: string) => void;
  /** The org's current slug when editing (always "available" to itself). */
  current?: string;
  disabled?: boolean;
  /** Reports whether the value can be submitted. */
  onStatus?: (status: SlugStatus) => void;
  hint?: string;
}

/** Hook form of the availability check, so forms can block submit on it. */
export function useSlugStatus(slug: string, current?: string) {
  const debounced = useDebouncedValue(slug, 300);
  const localProblem = slug ? slugProblem(slug) : null;
  const unchanged = current !== undefined && slug === current;
  const check = $api.useQuery(
    "get",
    "/api/v1/orgs/slug-available",
    { params: { query: { slug: debounced } } },
    { enabled: debounced !== "" && slugProblem(debounced) === null && !unchanged },
  );
  let status: SlugStatus = "idle";
  let reason: string | null = localProblem;
  if (slug === "" || unchanged) status = "idle";
  else if (localProblem) status = "unavailable";
  else if (debounced !== slug || check.isFetching) status = "checking";
  else if (check.isError) {
    status = "unavailable";
    reason = "check_failed";
  } else if (check.data) {
    status = check.data.available ? "available" : "unavailable";
    reason = check.data.reason ?? null;
  }
  return { status, message: reason ? (SLUG_MESSAGES[reason] ?? SLUG_MESSAGES.invalid_slug) : null };
}

export function SlugField({ id, value, onChange, current, disabled, hint }: SlugFieldProps) {
  const { status, message } = useSlugStatus(value, current);
  const describedBy = `${id}-status`;
  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>URL</Label>
      <div className="flex items-center rounded-sm border border-input focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 has-aria-invalid:border-destructive">
        <span className="pl-2.5 text-sm text-fg-faint select-none" aria-hidden>
          repo-sensei/
        </span>
        <Input
          id={id}
          value={value}
          disabled={disabled}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={status === "unavailable"}
          aria-describedby={describedBy}
          className="border-0 pl-0 shadow-none focus-visible:ring-0"
          onChange={(event) => {
            onChange(event.target.value.toLowerCase());
          }}
        />
        <span className="pr-2.5" aria-hidden>
          {status === "checking" && <Loader2 className="size-4 animate-spin text-fg-muted" />}
          {status === "available" && <Check className="size-4 text-ok" />}
          {status === "unavailable" && <X className="size-4 text-danger" />}
        </span>
      </div>
      <p
        id={describedBy}
        aria-live="polite"
        className={status === "unavailable" ? "text-sm text-danger" : "text-sm text-fg-muted"}
      >
        {status === "unavailable"
          ? message
          : status === "available"
            ? "Available."
            : status === "checking"
              ? "Checking…"
              : (hint ?? "Lowercase letters, digits and hyphens.")}
      </p>
    </div>
  );
}
