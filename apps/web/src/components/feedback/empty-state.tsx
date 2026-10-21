import { cn } from "cn";
import type { ReactNode } from "react";

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  /** The one obvious next step, usually a Button. */
  action?: ReactNode;
  /** h1 when the empty state is the whole page; h2 (default) inside one. */
  headingLevel?: 1 | 2;
  className?: string;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  headingLevel = 2,
  className,
}: EmptyStateProps) {
  const Heading = headingLevel === 1 ? "h1" : "h2";
  return (
    <section
      aria-label={title}
      className={cn(
        "flex flex-col items-center gap-3 rounded-xl border border-dashed border-line-strong p-10 text-center",
        className,
      )}
    >
      {icon && (
        <div aria-hidden className="text-fg-faint [&_svg]:size-8">
          {icon}
        </div>
      )}
      <Heading className="text-base font-semibold">{title}</Heading>
      {description && <p className="max-w-md text-sm text-fg-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </section>
  );
}
