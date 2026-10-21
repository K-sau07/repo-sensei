interface ComingSoonProps {
  title: string;
  description?: string;
}

/** Placeholder for pages that are routed but not built yet (the layout provides <main>). */
export function ComingSoon({ title, description }: ComingSoonProps) {
  return (
    <section
      aria-label={title}
      className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center"
    >
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="text-sm opacity-70">{description ?? "Coming soon."}</p>
    </section>
  );
}
