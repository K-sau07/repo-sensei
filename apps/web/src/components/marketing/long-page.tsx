/** A long-form public page (10.5: security, privacy, terms, blog posts): its title, a line of
 * context, and its MDX in the site's prose. */
export function LongPage({
  title,
  lead,
  updated,
  dated = "Last updated",
  children,
}: {
  title: string;
  lead?: string;
  /** When it last changed, or was published (ISO date). */
  updated?: string;
  dated?: "Last updated" | "Published";
  children: React.ReactNode;
}) {
  return (
    <article className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-6 py-12 sm:py-16">
      <header className="flex flex-col gap-2">
        <h1 className="text-4xl font-semibold tracking-[-0.02em]">{title}</h1>
        {lead && <p className="text-lg text-fg-muted">{lead}</p>}
        {updated && (
          <p className="text-sm text-fg-faint">
            {dated} <time dateTime={updated}>{DAY.format(new Date(updated))}</time>
          </p>
        )}
      </header>
      <div className="prose-sensei">{children}</div>
    </article>
  );
}

const DAY = new Intl.DateTimeFormat("en", { dateStyle: "long", timeZone: "UTC" });
