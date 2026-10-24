import { Suspense } from "react";

import { CodePathView } from "@/components/code/code-browser";

export default function CodePage() {
  return (
    <Suspense>
      <CodePathView />
    </Suspense>
  );
}
