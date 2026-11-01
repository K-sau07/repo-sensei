/**
 * The mock OAuth pages' API (7.2): a pending request from an editor on this computer
 * (`mock-request`), one that has expired (`mock-expired`), and a device code (`BCDF-GHJK`).
 * Approving sends the browser back to the editor's loopback callback with a code.
 */
import { http, HttpResponse } from "msw";

import { apiError } from "./errors";

const CALLBACK = "http://127.0.0.1:33418/callback";
const DEVICE_CODE = "BCDFGHJK";

const expired = (what: string) => apiError(400, "oauth_invalid_request", what);

export const oauthHandlers = [
  http.get<{ handle: string }>("*/api/v1/oauth/requests/:handle", ({ params }) => {
    if (params.handle !== "mock-request") {
      return expired("This sign-in request has expired. Start again from your editor.");
    }
    return HttpResponse.json({
      client_name: "Claude Code",
      client_uri: null,
      redirect_host: "127.0.0.1:33418",
      redirect_is_local: true,
      client_host: null,
      scopes: ["ask", "read:repo"],
      resource: `${location.origin}/mcp`,
    });
  }),

  http.post<{ handle: string }>(
    "*/api/v1/oauth/requests/:handle/decision",
    async ({ params, request }) => {
      if (params.handle !== "mock-request") return expired("This sign-in request has expired.");
      const body = (await request.json()) as { org: string; approve: boolean };
      const back = new URL(CALLBACK);
      if (body.approve) back.searchParams.set("code", `mock-code-${body.org}`);
      else back.searchParams.set("error", "access_denied");
      back.searchParams.set("state", "xyz");
      back.searchParams.set("iss", location.origin);
      return HttpResponse.json({ redirect_to: back.toString() });
    },
  ),

  http.get<{ code: string }>("*/api/v1/oauth/device/:code", ({ params }) => {
    if (params.code.toUpperCase().replace(/[^A-Z]/g, "") !== DEVICE_CODE) {
      return expired("That code isn't valid or has expired.");
    }
    return HttpResponse.json({
      client_name: "repo-sensei CLI",
      user_code: "BCDF-GHJK",
      scopes: ["ask", "read:repo"],
      resource: `${location.origin}/api`,
    });
  }),

  http.post("*/api/v1/oauth/device/decision", async ({ request }) => {
    const body = (await request.json()) as { user_code: string };
    if (body.user_code.toUpperCase().replace(/[^A-Z]/g, "") !== DEVICE_CODE) {
      return expired("That code isn't valid or has expired.");
    }
    return new HttpResponse(null, { status: 204 });
  }),
];
