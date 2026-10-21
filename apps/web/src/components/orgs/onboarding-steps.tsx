import { cn } from "cn";
import { Check } from "lucide-react";

const STEPS = [
  { title: "Organization", description: "Name your workspace" },
  { title: "Install the GitHub App", description: "Choose what it can read" },
  { title: "Pick repositories", description: "Start indexing" },
  { title: "Ask a first question", description: "See it work" },
];

/** Where the user is in onboarding. The first question arrives with Ask (a later release). */
export function OnboardingSteps({ current }: { current: number }) {
  return (
    <ol aria-label="Onboarding steps" className="flex flex-col gap-3">
      {STEPS.map((step, index) => {
        const state = index < current ? "done" : index === current ? "current" : "later";
        return (
          <li
            key={step.title}
            aria-current={state === "current" ? "step" : undefined}
            className={cn("flex items-start gap-3", state === "later" && "text-fg-faint")}
          >
            <span
              aria-hidden
              className={cn(
                "mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border text-xs tabular-nums",
                state === "current" && "border-fg bg-fg text-bg",
                state === "done" && "border-ok text-ok",
                state === "later" && "border-line-strong",
              )}
            >
              {state === "done" ? <Check className="size-3.5" /> : index + 1}
            </span>
            <span className="flex flex-col">
              <span className="text-sm font-medium">{step.title}</span>
              <span className="text-xs text-fg-muted">
                {state === "later" && index === STEPS.length - 1
                  ? "Available soon"
                  : step.description}
              </span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}
