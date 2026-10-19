import type { Metadata } from "next";

import { CreateOrgForm } from "@/components/orgs/create-org-form";
import { OnboardingSteps } from "@/components/orgs/onboarding-steps";

export const metadata: Metadata = { title: "Get started" };

export default function OnboardingPage() {
  return (
    <div className="mx-auto grid w-full max-w-4xl flex-1 gap-10 p-6 md:grid-cols-[14rem_1fr] md:p-12">
      <aside>
        <OnboardingSteps current={0} />
      </aside>
      <section aria-labelledby="onboarding-heading" className="flex max-w-md flex-col gap-6">
        <header className="flex flex-col gap-1">
          <h1 id="onboarding-heading" className="text-2xl font-semibold tracking-[-0.015em]">
            Create your organization
          </h1>
          <p className="text-sm text-fg-muted">
            Repositories, questions and fixes live in an organization. Invite your team once it
            exists.
          </p>
        </header>
        <CreateOrgForm />
      </section>
    </div>
  );
}
