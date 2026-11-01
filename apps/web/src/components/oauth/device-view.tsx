"use client";

/**
 * The device page (7.2): `sensei login` shows a code and this address; here the user types the
 * code (or arrives with it in `?code=`), sees what asks and for what, and allows it for one of
 * their orgs. The CLI, polling, then has its token.
 */
import { CircleCheck, CircleX, Terminal } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { $api, api } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { formatUserCode } from "@/lib/api/oauth";

import { firstOrg, GrantCard, OrgPicker, ScopeList } from "./grant-parts";

export function DeviceView() {
  const params = useSearchParams();
  const given = formatUserCode(params.get("code") ?? "");
  const [typed, setTyped] = useState(given);
  // Arrived with a whole code (`verification_uri_complete`): looked up at once.
  const [code, setCode] = useState<string | null>(given.length === 9 ? given : null);
  const request = $api.useQuery(
    "get",
    "/api/v1/oauth/device/{user_code}",
    { params: { path: { user_code: code ?? "" } } },
    { enabled: code !== null, retry: false },
  );
  const me = $api.useQuery("get", "/api/v1/me");
  const [org, setOrg] = useState<string | null>(null);
  const [sending, setSending] = useState<"allow" | "deny" | null>(null);
  const [done, setDone] = useState<"allowed" | "denied" | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  if (done) {
    return (
      <GrantCard>
        {done === "allowed" ? (
          <CircleCheck aria-hidden className="size-8 text-ok" />
        ) : (
          <CircleX aria-hidden className="size-8 text-fg-faint" />
        )}
        <h1 className="text-lg font-semibold" data-testid="device-done">
          {done === "allowed" ? "You're signed in on your device" : "Sign-in denied"}
        </h1>
        <p className="text-sm text-fg-muted">
          {done === "allowed"
            ? "Go back to your terminal: it carries on by itself. You can close this tab."
            : "Nothing was given to the device. You can close this tab."}
        </p>
      </GrantCard>
    );
  }

  const orgs = (me.data?.orgs ?? []).map((o) => ({ slug: o.slug, name: o.name }));
  const chosen = org ?? firstOrg(orgs, me.data?.last_org_slug);
  const answer = async (approve: boolean) => {
    if (code === null) return;
    setSending(approve ? "allow" : "deny");
    setFailed(null);
    try {
      await api.POST("/api/v1/oauth/device/decision", {
        body: { user_code: code, org: chosen, approve },
      });
      setDone(approve ? "allowed" : "denied");
    } catch (caught) {
      setFailed(caught instanceof ApiError ? caught.message : "Something went wrong.");
      setSending(null);
    }
  };

  const asked = code !== null && request.isSuccess ? request.data : null;
  return (
    <GrantCard>
      <Terminal aria-hidden className="size-8 text-fg-faint" />
      <header className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold">Sign in on a device</h1>
        <p className="text-sm text-fg-muted">
          Enter the code your terminal shows (from <span className="font-mono">sensei login</span>
          ).
        </p>
      </header>
      {asked === null ? (
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            setCode(formatUserCode(typed));
          }}
        >
          <Input
            aria-label="Code"
            value={typed}
            autoComplete="off"
            spellCheck={false}
            placeholder="XXXX-XXXX"
            className="font-mono tracking-widest uppercase"
            onChange={(event) => {
              setTyped(formatUserCode(event.target.value));
            }}
          />
          <Button type="submit" disabled={typed.length !== 9 || request.isFetching}>
            Continue
          </Button>
        </form>
      ) : null}
      {code !== null && request.isError ? (
        <p role="alert" className="text-sm text-danger" data-testid="device-error">
          {request.error instanceof ApiError && request.error.status === 400
            ? "That code isn't valid or has expired. Check it, or run sensei login again."
            : "Couldn't check the code. Try again."}
        </p>
      ) : null}
      {asked ? (
        <>
          <p className="text-sm">
            <span className="font-medium">{asked.client_name}</span> asks to use repo-sensei as @
            {me.data?.login} with code <span className="font-mono">{asked.user_code}</span>.
          </p>
          <ScopeList scopes={asked.scopes} />
          {orgs.length > 0 ? <OrgPicker orgs={orgs} value={chosen} onChange={setOrg} /> : null}
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
        </>
      ) : null}
    </GrantCard>
  );
}
