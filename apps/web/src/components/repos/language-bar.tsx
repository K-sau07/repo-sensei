import { cn } from "cn";

const COLORS = ["bg-chart-1", "bg-chart-2", "bg-chart-3", "bg-chart-4", "bg-chart-5"];
const SHOWN = 4;

export interface LanguageShare {
  language: string;
  share: number; // 0..1
  color: string;
}

/** The biggest languages by bytes, the rest as "Other". */
export function languageShares(languages: Record<string, number>): LanguageShare[] {
  const entries = Object.entries(languages)
    .filter(([, bytes]) => bytes > 0)
    .sort(([, a], [, b]) => b - a);
  const total = entries.reduce((sum, [, bytes]) => sum + bytes, 0);
  if (total === 0) return [];
  const top = entries.slice(0, SHOWN);
  const rest = entries.slice(SHOWN).reduce((sum, [, bytes]) => sum + bytes, 0);
  const shares = top.map(([language, bytes], index) => ({
    language,
    share: bytes / total,
    color: COLORS[index] ?? "bg-chart-5",
  }));
  if (rest > 0) shares.push({ language: "Other", share: rest / total, color: "bg-fg-faint" });
  return shares;
}

const percent = (share: number) => `${(share * 100).toFixed(share < 0.1 ? 1 : 0)}%`;

/** A thin stacked bar of the languages, with the shares spelled out for screen readers (and, with
 * `legend`, on screen). */
export function LanguageBar({
  languages,
  legend = false,
  className,
}: {
  languages: Record<string, number>;
  legend?: boolean;
  className?: string;
}) {
  const shares = languageShares(languages);
  if (shares.length === 0) return <span className="text-xs text-fg-faint">No code yet</span>;
  const summary = shares.map((s) => `${s.language} ${percent(s.share)}`).join(", ");
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", className)}>
      <div role="img" aria-label={summary} className="flex h-1.5 overflow-hidden rounded-full">
        {shares.map((s) => (
          <span key={s.language} className={s.color} style={{ width: `${s.share * 100}%` }} />
        ))}
      </div>
      {legend && (
        <ul aria-hidden className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-fg-muted">
          {shares.map((s) => (
            <li key={s.language} className="inline-flex items-center gap-1.5">
              <span className={cn("size-2 rounded-full", s.color)} />
              {s.language} <span className="text-fg-faint tabular-nums">{percent(s.share)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
