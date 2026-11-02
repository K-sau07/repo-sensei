/**
 * The mock API keys (7.5), with the API's rules: a member sees and revokes their own keys, an
 * admin everyone's; the key is in the answer to POST only; keys get read:repo and ask, never more.
 * Seeded: Octo's working "laptop" key, a revoked one, and Hubot's "ci" key.
 */
import { http, HttpResponse } from "msw";

import type { ApiKey, ApiKeyCreated } from "@/lib/api/types";
import { HUBOT, NOW, OCTO, atLeast, db, saveDb, type MockApiKey } from "@/mocks/db";

import { apiError } from "./errors";
import { orgAccess } from "./orgs";

const GRANTABLE = new Set(["read:repo", "ask"]);
const EXPIRY_DAYS = new Set([30, 90, 365]);
const ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

function seedKeys(): MockApiKey[] {
  const key = (over: Partial<MockApiKey> & Pick<MockApiKey, "id" | "name">): MockApiKey => ({
    org: "acme",
    user_id: OCTO,
    prefix: "AbCd1234",
    scopes: ["read:repo"],
    created_at: "2026-10-10T12:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
    ...over,
  });
  return [
    key({
      id: "0199f000-0000-7000-8000-0000000000c1",
      name: "laptop",
      scopes: ["read:repo", "ask"],
      last_used_at: NOW,
      expires_at: "2027-01-08T12:00:00Z",
    }),
    key({
      id: "0199f000-0000-7000-8000-0000000000c2",
      name: "old ci",
      prefix: "ZyXw9876",
      revoked_at: "2026-10-12T12:00:00Z",
    }),
    key({
      id: "0199f000-0000-7000-8000-0000000000c3",
      name: "ci",
      user_id: HUBOT,
      prefix: "Hb0tK3y5",
      created_at: "2026-10-11T12:00:00Z",
    }),
  ];
}

const keys = () => (db.apiKeys ??= seedKeys());

const random = (n: number) =>
  Array.from({ length: n }, () => ALPHABET[Math.floor(Math.random() * ALPHABET.length)]).join("");

function shown(key: MockApiKey, me: string): ApiKey {
  const { org: _org, user_id: owner, ...rest } = key;
  return {
    ...rest,
    mine: owner === me,
    owner_login: db.users.find((u) => u.id === owner)?.login ?? null,
  };
}

export const apiKeyHandlers = [
  http.get<{ org: string }>("*/api/v1/orgs/:org/api-keys", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const admin = atLeast(access.role, "admin");
    const found = keys().filter(
      (k) => k.org === params.org && (admin || k.user_id === access.user.id),
    );
    return HttpResponse.json({ keys: found.map((k) => shown(k, access.user.id)) });
  }),

  http.post<{ org: string }>("*/api/v1/orgs/:org/api-keys", async ({ params, request }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const body = (await request.json()) as {
      name: string;
      scopes: string[];
      expires_in_days: number | null;
    };
    if (body.scopes.length === 0 || body.scopes.some((s) => !GRANTABLE.has(s))) {
      return apiError(422, "invalid_scopes", "Keys can have read:repo and ask.");
    }
    if (body.expires_in_days !== null && !EXPIRY_DAYS.has(body.expires_in_days)) {
      return apiError(422, "invalid_expiry", "Keys expire after 30, 90 or 365 days, or never.");
    }
    const prefix = random(8);
    const created = new Date();
    const expires = body.expires_in_days
      ? new Date(created.getTime() + body.expires_in_days * 86_400_000).toISOString()
      : null;
    const key: MockApiKey = {
      id: crypto.randomUUID(),
      org: params.org,
      user_id: access.user.id,
      name: body.name,
      prefix,
      scopes: [...body.scopes].sort(),
      created_at: created.toISOString(),
      last_used_at: null,
      expires_at: expires,
      revoked_at: null,
    };
    keys().unshift(key);
    saveDb();
    return HttpResponse.json<ApiKeyCreated>(
      { ...shown(key, access.user.id), key: `rs_${prefix}_${random(43)}` },
      { status: 201 },
    );
  }),

  http.delete<{ org: string; id: string }>("*/api/v1/orgs/:org/api-keys/:id", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const key = keys().find((k) => k.id === params.id && k.org === params.org);
    const allowed = key && (key.user_id === access.user.id || atLeast(access.role, "admin"));
    if (!key || !allowed) return apiError(404, "api_key_not_found", "No such API key.");
    key.revoked_at ??= new Date().toISOString();
    saveDb();
    return new HttpResponse(null, { status: 204 });
  }),
];
