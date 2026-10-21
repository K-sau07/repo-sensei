import { ComingSoon } from "@/components/coming-soon";

export default function NotFound() {
  return (
    <main className="flex flex-1 flex-col">
      <ComingSoon title="Page not found" description="There is nothing at this address." />
    </main>
  );
}
