/** A fix's status and risk, each a word with its tone (8.11). */
import { cn } from "cn";

import {
  RISK_LABELS,
  riskTone,
  STATUS_LABELS,
  statusTone,
  type FixRisk,
  type FixStatus,
  type Tone,
} from "@/lib/api/fixes";

const TONES: Record<Tone, string> = {
  ok: "border-ok/40 text-ok",
  warn: "border-warn/40 text-warn",
  danger: "border-danger/40 text-danger",
  info: "border-signal/40 text-signal",
  neutral: "border-line text-fg-muted",
};

function Pill({ tone, children, testId }: { tone: Tone; children: string; testId: string }) {
  return (
    <span
      data-testid={testId}
      data-tone={tone}
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs whitespace-nowrap",
        TONES[tone],
      )}
    >
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: FixStatus }) {
  return (
    <Pill tone={statusTone(status)} testId="fix-status">
      {STATUS_LABELS[status]}
    </Pill>
  );
}

export function RiskBadge({ risk }: { risk: FixRisk }) {
  return (
    <Pill tone={riskTone(risk)} testId="fix-risk">
      {RISK_LABELS[risk]}
    </Pill>
  );
}
