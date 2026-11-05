"use client";

/**
 * An org's models (admins, 10.7.2): for answers, diagrams and fixes, what each runs on as
 * recommended (what the accuracy checks run on) and a model and an effort it may be raised to.
 * A choice is a floor, so a step never runs below recommended. Each change is saved as it is
 * made. Summaries made while indexing aren't a choice: they're kept from commit to commit.
 */
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { ErrorState } from "@/components/feedback/error-state";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { roleAtLeast, useOrgRole } from "@/hooks/use-org-role";
import { $api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import {
  effortLabel,
  modelName,
  recommended,
  STEP_LABELS,
  tierLabel,
  type EffortChoice,
  type ModelsOut,
  type StepOut,
  type TierChoice,
} from "@/lib/orgs/models";

const PATH = "/api/v1/orgs/{org}/settings/models";

export function ModelSettings() {
  const { slug, role, me } = useOrgRole();
  if (me.isError) {
    return <ErrorState title="Couldn't load the organization" onRetry={() => void me.refetch()} />;
  }
  if (role === null) return <Skeleton className="h-40" role="status" aria-label="Loading" />;
  if (!roleAtLeast(role, "admin")) {
    return (
      <p className="text-sm text-fg-muted">
        Only the organization&apos;s admins choose its models.
      </p>
    );
  }
  return <ModelsView org={slug} />;
}

function ModelsView({ org }: { org: string }) {
  const queryClient = useQueryClient();
  const options = { params: { path: { org } } };
  const models = $api.useQuery("get", PATH, options);
  const key = $api.queryOptions("get", PATH, options).queryKey;
  const save = $api.useMutation("patch", PATH, {
    onSuccess: (data) => {
      queryClient.setQueryData(key, data);
    },
  });
  if (models.isError) {
    return <ErrorState title="Models didn't load" onRetry={() => void models.refetch()} />;
  }
  if (!models.data) {
    return (
      <div role="status" aria-label="Loading models" className="flex flex-col gap-4">
        <Skeleton className="h-28" />
        <Skeleton className="h-28" />
      </div>
    );
  }
  const data = models.data;

  const change = async (step: StepOut, next: { tier?: TierChoice; effort?: EffortChoice }) => {
    const choice = { tier: next.tier ?? step.tier, effort: next.effort ?? step.effort };
    try {
      await save.mutateAsync({ ...options, body: { [step.step]: choice } });
      toast.success(`${STEP_LABELS[step.step]} updated`);
    } catch (caught) {
      toast.error(caught instanceof ApiError ? caught.message : "The change wasn't saved.");
    }
  };

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-1">
        <h2 className="font-medium">Models</h2>
        <p className="text-sm text-fg-muted">
          Each step runs as recommended: the models and effort its accuracy is checked on. You can
          raise a step to a larger model or more effort, which costs more; it never runs below
          recommended.
        </p>
      </div>
      {data.steps.map((step) => (
        <StepRow
          key={step.step}
          step={step}
          models={data.models}
          busy={save.isPending}
          onChange={(next) => void change(step, next)}
        />
      ))}
      <p className="text-sm text-fg-muted">
        Summaries made while indexing always run on {modelName(data.models.fast ?? "")}: they are
        kept from commit to commit, and one model keeps them alike.
      </p>
    </div>
  );
}

function StepRow({
  step,
  models,
  busy,
  onChange,
}: {
  step: StepOut;
  models: ModelsOut["models"];
  busy: boolean;
  onChange: (next: { tier?: TierChoice; effort?: EffortChoice }) => void;
}) {
  const label = STEP_LABELS[step.step];
  const titleId = `models-${step.step}`;
  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-3 border-t border-border pt-6">
      <div className="flex flex-col gap-1">
        <h3 id={titleId} className="text-sm font-medium">
          {label}
        </h3>
        <p className="text-sm text-fg-muted">
          <span className="text-fg">Recommended:</span> {recommended(step.step, models)}
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <Select
          value={step.tier}
          disabled={busy}
          onValueChange={(value) => {
            if (value !== null && value !== step.tier) onChange({ tier: value });
          }}
        >
          <SelectTrigger aria-label={`${label}: model`} size="sm" className="w-56">
            <SelectValue>{tierLabel(step.tier, models)}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {step.tiers.map((tier) => (
              <SelectItem key={tier} value={tier}>
                {tierLabel(tier, models)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={step.effort}
          disabled={busy}
          onValueChange={(value) => {
            if (value !== null && value !== step.effort) onChange({ effort: value });
          }}
        >
          <SelectTrigger aria-label={`${label}: effort`} size="sm" className="w-48">
            <SelectValue>{effortLabel(step.effort)}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            {step.efforts.map((effort) => (
              <SelectItem key={effort} value={effort}>
                {effortLabel(effort)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </section>
  );
}
