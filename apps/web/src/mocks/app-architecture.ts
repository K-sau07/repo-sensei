/**
 * The mock whole-app architecture (6.6): the full-stack template's modules as the API draws them
 * from its graph (each a folder, in the tier most of its code is in, with what it holds), and the
 * graph's edges between them, counted. Every line is the graph's: verified.
 */
import type { Spec, SpecEdge, SpecMember, SpecNode } from "@/lib/api/diagrams";

function part(id: string, layer: string, members: SpecMember[], more = 0): SpecNode {
  return {
    id,
    label: id,
    layer,
    kind: "module",
    citation: null,
    members,
    more_members: more,
  };
}

const m = (name: string, kind: string): SpecMember => ({ name, kind });

function line(from: string, to: string, type: string, count: number, verb: string): SpecEdge {
  return {
    from,
    to,
    type,
    label: count > 1 ? `${String(count)} ${verb}` : verb,
    verified: true,
    confidence: 0.9,
    provenance: "graph",
    inferred: null,
  };
}

const ROUTES = "frontend/src/routes";
const ITEMS_UI = "frontend/src/components/Items";
const HOOKS = "frontend/src/hooks";
const CLIENT = "frontend/src/client";
const API = "backend/app/api/routes";
const CORE = "backend/app/core";
const APP = "backend/app";

export function appArchitecture(title: string): Spec {
  return {
    title,
    kind: "architecture",
    nodes: [
      part(ROUTES, "ui", [
        m("Login", "component"),
        m("SignUp", "component"),
        m("Items", "component"),
        m("Admin", "component"),
        m("RecoverPassword", "component"),
      ]),
      part(ITEMS_UI, "ui", [
        m("AddItem", "component"),
        m("EditItem", "component"),
        m("DeleteItem", "component"),
      ]),
      part(HOOKS, "state", [m("useAuth", "hook"), m("useCustomToast", "hook")]),
      part(CLIENT, "client", [
        m("LoginService", "class"),
        m("ItemsService", "class"),
        m("UsersService", "class"),
      ]),
      part(
        API,
        "route",
        [
          m("POST /api/v1/login/access-token", "endpoint"),
          m("GET /api/v1/items/", "endpoint"),
          m("POST /api/v1/items/", "endpoint"),
          m("PUT /api/v1/items/{id}", "endpoint"),
          m("GET /api/v1/users/me", "endpoint"),
          m("POST /api/v1/users/signup", "endpoint"),
        ],
        9,
      ),
      part(CORE, "service", [m("Settings", "class"), m("create_access_token", "function")]),
      part(APP, "db", [
        m("user", "table"),
        m("item", "table"),
        m("User", "model"),
        m("Item", "model"),
      ]),
    ],
    edges: [
      line(ROUTES, ITEMS_UI, "renders", 6, "renders"),
      line(ROUTES, HOOKS, "uses_hook", 5, "uses"),
      line(HOOKS, CLIENT, "calls", 4, "calls"),
      line(ROUTES, CLIENT, "calls", 3, "calls"),
      line(ITEMS_UI, CLIENT, "calls", 3, "calls"),
      line(CLIENT, API, "targets", 18, "requests"),
      line(API, APP, "calls", 14, "calls"),
      line(API, CORE, "injects", 7, "injects"),
      line(CORE, APP, "reads", 3, "reads"),
    ],
  };
}
