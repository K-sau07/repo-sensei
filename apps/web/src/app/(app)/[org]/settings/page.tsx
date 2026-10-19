import { Suspense } from "react";

import { RedirectToGeneral } from "@/components/orgs/redirect-to-general";

/** /[org]/settings opens the first section. */
export default function OrgSettingsIndex() {
  return (
    <Suspense>
      <RedirectToGeneral />
    </Suspense>
  );
}
