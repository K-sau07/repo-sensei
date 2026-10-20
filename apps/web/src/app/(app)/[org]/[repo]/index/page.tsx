import type { Metadata } from "next";
import { Suspense } from "react";

import { IndexHistory } from "@/components/indexing/index-pages";

export const metadata: Metadata = { title: "Index" };

export default function IndexPage() {
  return (
    <Suspense>
      <IndexHistory />
    </Suspense>
  );
}
