import type { Metadata } from "next";

import { LongPage } from "@/components/marketing/long-page";
import About from "@/content/about.mdx";
import { SITE_CARD } from "@/lib/site/author";

const LEAD = "repo-sensei is built by Saurabh Kashyap.";

export const metadata: Metadata = {
  title: "About",
  description:
    "Who builds repo-sensei: Saurabh Kashyap, a software engineer with an M.S. from Northeastern University.",
  alternates: { canonical: "/about" },
  openGraph: { title: "About repo-sensei", url: "/about", images: [SITE_CARD] },
};

/** Who builds repo-sensei and why, with where to find him. */
export default function AboutPage() {
  return (
    <LongPage title="About" lead={LEAD} updated="2026-11-20">
      <About />
    </LongPage>
  );
}
