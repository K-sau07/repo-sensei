import { AppShell } from "@/components/shell/app-shell";
import { WebVitals } from "@/components/shell/web-vitals";

export default function OrgLayout({ children }: LayoutProps<"/[org]">) {
  return (
    <AppShell>
      <WebVitals />
      {children}
    </AppShell>
  );
}
