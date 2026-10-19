"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState, type SubmitEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { slugify, SLUG_MESSAGES } from "@/lib/orgs/slug";
import { queryKeys } from "@/lib/query/keys";
import { orgPath } from "@/lib/routes";

import { SlugField, useSlugStatus } from "./slug-field";

/** Onboarding step 1: name the organization; the URL follows the name until edited by hand. */
export function CreateOrgForm() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const { status } = useSlugStatus(slug);

  const canSubmit = name.trim() !== "" && status === "available" && !submitting;

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const { data } = await api.POST("/api/v1/orgs", { body: { name: name.trim(), slug } });
      await queryClient.invalidateQueries({ queryKey: queryKeys.me() });
      router.push(orgPath(data?.slug ?? slug));
    } catch (caught) {
      const message =
        caught instanceof ApiError
          ? (SLUG_MESSAGES[caught.code] ?? caught.message)
          : "Couldn't create the organization. Please try again.";
      setError(message);
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-5" noValidate>
      <div className="grid gap-2">
        <Label htmlFor="org-name">Organization name</Label>
        <Input
          id="org-name"
          value={name}
          maxLength={100}
          placeholder="Acme Inc"
          autoComplete="organization"
          onChange={(event) => {
            setName(event.target.value);
            if (!slugEdited) setSlug(slugify(event.target.value));
          }}
        />
      </div>
      <SlugField
        id="org-slug"
        value={slug}
        onChange={(value) => {
          setSlugEdited(true);
          setSlug(value);
        }}
        hint="Your workspace address. You can change it later."
      />
      {error && (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <Button type="submit" size="lg" disabled={!canSubmit}>
        {submitting ? "Creating…" : "Create organization"}
      </Button>
    </form>
  );
}
