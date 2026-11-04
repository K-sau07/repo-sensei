import type { Metadata } from "next";

import { RELEASES } from "@/content/changelog";

const DESCRIPTION = "What changed in repo-sensei, newest first.";

export const metadata: Metadata = {
  title: "Changelog",
  description: DESCRIPTION,
  alternates: { canonical: "/changelog" },
};

const DAY = new Intl.DateTimeFormat("en", { dateStyle: "long", timeZone: "UTC" });

export default function ChangelogPage() {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-10 px-6 py-12 sm:py-16">
      <header className="flex flex-col gap-2">
        <h1 className="text-4xl font-semibold tracking-[-0.02em]">Changelog</h1>
        <p className="text-lg text-fg-muted">{DESCRIPTION}</p>
      </header>
      <ol className="flex flex-col gap-10">
        {RELEASES.map((release) => (
          <li
            key={`${release.date}-${release.title}`}
            className="grid gap-2 sm:grid-cols-[9rem_1fr]"
          >
            <time dateTime={release.date} className="text-sm text-fg-muted">
              {DAY.format(new Date(release.date))}
            </time>
            <section aria-label={release.title} className="flex flex-col gap-2">
              <h2 className="font-semibold">{release.title}</h2>
              <ul className="flex list-disc flex-col gap-1.5 pl-5 text-sm text-fg-muted">
                {release.changes.map((change) => (
                  <li key={change}>{change}</li>
                ))}
              </ul>
            </section>
          </li>
        ))}
      </ol>
    </div>
  );
}
