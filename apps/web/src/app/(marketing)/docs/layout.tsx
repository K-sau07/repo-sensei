import { DocsNav } from "@/components/docs/docs-nav";
import { DocsSearch } from "@/components/docs/docs-search";

/** The docs (10.5): search and the pages' sidebar beside each page. */
export default function DocsLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-10 px-6 py-10 md:grid-cols-[14rem_minmax(0,1fr)]">
      <aside className="flex flex-col gap-6 md:sticky md:top-20 md:self-start">
        <DocsSearch />
        <DocsNav />
      </aside>
      <div className="min-w-0">{children}</div>
    </div>
  );
}
