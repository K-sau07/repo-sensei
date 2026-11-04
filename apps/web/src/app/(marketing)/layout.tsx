import { ConstellationBackdrop } from "@/components/auth/constellation-backdrop";
import { SiteFooter } from "@/components/marketing/site-footer";
import { SiteHeader } from "@/components/marketing/site-header";

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <>
      {/* The sign-in page's constellation, behind every public page (still, and faint). */}
      <div aria-hidden className="site-backdrop pointer-events-none fixed inset-0 -z-30">
        <ConstellationBackdrop />
      </div>
      <SiteHeader />
      <main className="flex flex-1 flex-col">{children}</main>
      <SiteFooter />
    </>
  );
}
