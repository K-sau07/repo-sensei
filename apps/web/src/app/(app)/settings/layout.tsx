import { SettingsNav } from "@/components/settings/settings-nav";

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <SettingsNav />
      <main className="mx-auto w-full max-w-3xl flex-1 px-6 py-8">{children}</main>
    </>
  );
}
