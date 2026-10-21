import { Suspense } from "react";

import { OrgSettingsNav } from "@/components/orgs/org-settings-nav";

export default function OrgSettingsLayout({ children }: LayoutProps<"/[org]/settings">) {
  return (
    <div className="mx-auto grid w-full max-w-5xl gap-8 p-8 md:grid-cols-[12rem_1fr]">
      <div className="flex flex-col gap-4">
        <h1 className="text-xl font-semibold tracking-[-0.015em]">Settings</h1>
        <Suspense>
          <OrgSettingsNav />
        </Suspense>
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}
