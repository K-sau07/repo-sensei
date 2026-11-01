"use client";

/**
 * The OAuth consent page (7.2): an editor's MCP client (or another app) asks to act as you. It
 * shows who asks, what for, which org, and where the answer goes (its host always; a warning
 * when that is this computer, since any local app could claim a name), then sends the browser
 * back to the app with a code, or with "access denied".
 *
 * Reached from `GET /api/v1/oauth/authorize` with `?request=` (the pending request), or with
 * `?error=` when the request itself was bad (its app isn't trusted to be sent anything).
 */
import { KeyRound, TriangleAlert } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { ErrorState } from "@/components/feedback/error-state";
import { DelayedSpinner } from "@/components/primitives/delayed-spinner";
import { Button } from "@/components/ui/button";
import { $api, api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";

import { firstOrg, GrantCard, OrgPicker, ScopeList } from "./grant-parts";

export function ConsentView() {
  const params = useSearchParams();
  const handle = params.get("request") ?? "";
  const error = params.get("error");
  const request = $api.useQuery(
    "get",
    "/api/v1/oauth/requests/{handle}",
    { params: { path: { handle } } },
    { enabled: handle !== "" && error === null, retry: false },
  );
  const me = $api.useQuery("get", "/api/v1/me");
  const [org, setOrg] = useState<string | null>(null);
  const [sending, setSending] = useState<"allow" | "deny" | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  if (error !== null || handle === "") {
    return (
      <GrantCard>
        <TriangleAlert aria-hidden className="size-8 text-warn" />
        <h1 className="text-lg font-semibold">This sign-in can&rsquo;t continue</h1>
        <p className="text-sm text-fg-muted" data-testid="consent-error">
          {params.get("error_description") ?? "The app's request was missing or wrong."} Start again
          from the app.
        </p>
      </GrantCard>
    );
  }
  if (request.isPending || me.isPending) return <DelayedSpinner active label="Loading" />;
  if (request.isError) {
    const gone = request.error instanceof ApiError && request.error.status === 400;
    return gone ? (
      <GrantCard>
        <h1 className="text-lg font-semibold">This sign-in request has expired</h1>
        <p className="text-sm text-fg-muted" data-testid="consent-error">
          Requests last 10 minutes and are answered once. Start again from the app.
        </p>
      </GrantCard>
    ) : (
      <ErrorState title="Couldn't load the request" onRetry={() => void request.refetch()} />
    );
  }
  if (me.isError) {
    return <ErrorState title="Couldn't load your account" onRetry={() => void me.refetch()} />;
  }

  const asked = request.data;
  const orgs = me.data.orgs.map((o) => ({ slug: o.slug, name: o.name }));
  const chosen = org ?? firstOrg(orgs, me.data.last_org_slug);

  const answer = async (approve: boolean) => {
    setSending(approve ? "allow" : "deny");
    setFailed(null);
    try {
      const { data } = await api.POST("/api/v1/oauth/requests/{handle}/decision", {
        params: { path: { handle } },
        body: { org: chosen, approve },
      });
      if (data) window.location.assign(data.redirect_to);
    } catch (caught) {
      setFailed(caught instanceof ApiError ? caught.message : "Something went wrong.");
      setSending(null);
    }
  };

  return (
    <GrantCard>
      <KeyRound aria-hidden className="size-8 text-fg-faint" />
      <header className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold">
          {asked.client_name} wants to use repo-sensei as @{me.data.login}
        </h1>
        {asked.client_host ? (
          <p className="text-xs text-fg-muted">Published at {asked.client_host}</p>
        ) : null}
      </header>
      <ScopeList scopes={asked.scopes} />
      {orgs.length > 0 ? (
        <OrgPicker orgs={orgs} value={chosen} onChange={setOrg} />
      ) : (
        <p className="text-sm text-danger">You aren&rsquo;t in any organization yet.</p>
      )}
      <div
        className={
          asked.redirect_is_local
            ? "rounded-lg border border-warn/30 bg-warn/8 p-3 text-sm"
            : "text-sm text-fg-muted"
        }
        data-testid="consent-redirect"
      >
        You&rsquo;ll be sent back to <span className="font-mono">{asked.redirect_host}</span>.
        {asked.redirect_is_local
          ? " That's an app on this computer: allow only if you just started this sign-in from it."
          : null}
      </div>
      {failed ? (
        <p role="alert" className="text-sm text-danger">
          {failed}
        </p>
      ) : null}
      <div className="flex gap-2">
        <Button
          size="lg"
          className="flex-1"
          disabled={sending !== null || orgs.length === 0}
          onClick={() => void answer(true)}
        >
          {sending === "allow" ? "Allowing…" : "Allow"}
        </Button>
        <Button
          size="lg"
          variant="outline"
          disabled={sending !== null}
          onClick={() => void answer(false)}
        >
          {sending === "deny" ? "Denying…" : "Deny"}
        </Button>
      </div>
    </GrantCard>
  );
}
