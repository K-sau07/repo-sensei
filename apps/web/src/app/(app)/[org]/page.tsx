import { Suspense } from "react";

import { OrgHome } from "@/components/orgs/org-home";

export default function OrgHomePage() {
  return (
    <Suspense>
      <OrgHome />
    </Suspense>
  );
}
