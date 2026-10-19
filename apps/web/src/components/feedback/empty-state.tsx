import { cn } from "cn";
import type { ReactNode } from "react";

interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  /** The one obvious next step, usually a Button. */
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
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
      <h2 className="text-base font-semibold">{title}</h2>
      {description && <p className="max-w-md text-sm text-fg-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </section>
  );
}
