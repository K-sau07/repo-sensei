import type { Metadata } from "next";

import { LongPage } from "@/components/marketing/long-page";
import SecurityModel from "@/content/docs/security-model.mdx";

const LEAD = "Who can read what, how code is kept apart, and what runs where.";

export const metadata: Metadata = {
  title: "Security",
  description: LEAD,
  alternates: { canonical: "/security" },
};

/** The same page as the docs' security model, for those who look for it here. */
export default function SecurityPage() {
  return (
    <LongPage title="Security" lead={LEAD} updated="2026-11-10">
      <SecurityModel />
    </LongPage>
  );
}
