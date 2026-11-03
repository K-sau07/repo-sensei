import type { Metadata } from "next";
import { Suspense } from "react";

import { Gallery } from "@/components/explore/gallery";
import { Skeleton } from "@/components/ui/skeleton";
import { publicRepos } from "@/lib/api/explore-server";

const DESCRIPTION =
  "Open-source repositories, indexed by repo-sensei: what each one does, how it's laid out, and answers that cite their lines.";

export const metadata: Metadata = {
  title: "Explore",
  description: DESCRIPTION,
  alternates: { canonical: "/explore" },
  openGraph: { title: "Explore · repo-sensei", description: DESCRIPTION, url: "/explore" },
};

export default function ExplorePage() {
  return (
    <Suspense fallback={<Waiting />}>
      <Listed />
    </Suspense>
  );
}

async function Listed() {
  return <Gallery repos={await publicRepos()} />;
}

function Waiting() {
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-4 px-6 py-10 sm:grid-cols-2 lg:grid-cols-3">
      {Array.from({ length: 6 }, (_, i) => (
        <Skeleton key={i} className="h-44 rounded-lg" />
      ))}
    </div>
  );
}
