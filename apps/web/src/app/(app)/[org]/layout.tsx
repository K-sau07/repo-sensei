import { AppShell } from "@/components/shell/app-shell";

export default function OrgLayout({ children }: LayoutProps<"/[org]">) {
  return <AppShell>{children}</AppShell>;
}
