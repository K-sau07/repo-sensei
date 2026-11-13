/**
 * The mock whole app (6.6): the full-stack template's architecture as the API draws it (its
 * modules grouped and named into parts, each a box with its icon and what it does; the outside
 * systems its code imports; the graph's lines between them, counted; the user's line inferred),
 * and its schema as an ER diagram (its tables, columns, keys and the foreign key between them).
 */
import type { Spec, SpecEdge, SpecMember, SpecNode } from "@/lib/api/diagrams";

const ROUTES = "frontend/src/routes";
const ITEMS_UI = "frontend/src/components/Items";
const HOOKS = "frontend/src/hooks";
const CLIENT = "frontend/src/client";
const API = "backend/app/api/routes";
const CORE = "backend/app/core";
const APP = "backend/app";

const folders = (...names: string[]): SpecMember[] =>
  names.map((name) => ({ name, kind: "module" }));

function part(
  id: string,
  label: string,
  layer: string,
  icon: string,
  note: string,
  modules: SpecMember[],
): SpecNode {
  return { id, label, layer, kind: layer, citation: null, members: modules, note, icon };
}

function outside(id: string, label: string, kind: string): SpecNode {
  return { id, label, layer: "outside", kind, citation: null };
}

type Call = [string, string, string, number];

function line(
  from: string,
  to: string,
  type: string,
  label: string,
  count: number,
  calls: Call[] = [],
): SpecEdge {
  return {
    from,
    to,
    type,
    label,
    verified: true,
    confidence: 0.9,
    provenance: "graph",
    inferred: null,
    count,
    samples: calls.map(([src, dst, path, at]) => ({ from: src, to: dst, path, line: at })),
  };
}

const held = (...items: [string, string][]): SpecMember[] =>
  items.map(([kind, name]) => ({ name, kind }));

export function appArchitecture(title: string): Spec {
  return {
    title,
    kind: "architecture",
    summary:
      "A full-stack items app: a React frontend calls a FastAPI backend that signs users in and keeps their items in PostgreSQL.",
    steps: [
      { from: "actor:user", to: "part:web-app", text: "A user signs in and opens their items." },
      {
        from: "part:web-app",
        to: "part:api-client",
        text: "The page calls the generated client: `LoginService.loginAccessToken`, `ItemsService.createItem`.",
      },
      {
        from: "part:api-client",
        to: "part:rest-api",
        text: "The client sends the HTTP request, e.g. `POST /api/v1/items/`.",
      },
      {
        from: "part:rest-api",
        to: "part:auth-security",
        text: "Each endpoint first checks the session through `get_current_user`.",
      },
      {
        from: "part:rest-api",
        to: "part:data-models",
        text: "The endpoint reads or saves the rows: `authenticate`, `create_item`.",
      },
      {
        from: "part:data-models",
        to: "outside:postgresql",
        text: "The models are stored in PostgreSQL.",
      },
    ],
    nodes: [
      {
        id: "actor:user",
        label: "User",
        layer: "actor",
        kind: "actor",
        citation: null,
        icon: "user",
      },
      part(
        "part:web-app",
        "Web app",
        "frontend",
        "monitor",
        "Pages to sign in, sign up, and manage items and users.",
        [
          ...folders(ROUTES, ITEMS_UI, HOOKS),
          ...held(
            ["component", "Login"],
            ["component", "SignUp"],
            ["component", "Items"],
            ["component", "AddItem"],
            ["hook", "useAuth"],
          ),
        ],
      ),
      part(
        "part:api-client",
        "API client",
        "frontend",
        "plug",
        "Generated services the pages call the backend through.",
        [
          ...folders(CLIENT),
          ...held(["class", "LoginService"], ["class", "ItemsService"], ["class", "UsersService"]),
        ],
      ),
      part(
        "part:rest-api",
        "REST API",
        "api",
        "server",
        "Endpoints for login, users, items and password recovery.",
        [
          ...folders(API),
          ...held(
            ["endpoint", "POST /api/v1/login/access-token"],
            ["endpoint", "GET /api/v1/items/"],
            ["endpoint", "POST /api/v1/items/"],
            ["endpoint", "GET /api/v1/users/me"],
          ),
        ],
      ),
      part(
        "part:auth-security",
        "Auth & security",
        "service",
        "shield",
        "Settings, password hashing and access tokens.",
        [
          ...folders(CORE),
          ...held(
            ["class", "Settings"],
            ["function", "create_access_token"],
            ["function", "verify_password"],
          ),
        ],
      ),
      part(
        "part:data-models",
        "Data models",
        "data",
        "database",
        "User and Item models and the code that reads and writes them.",
        [
          ...folders(APP),
          ...held(
            ["table", "user"],
            ["table", "item"],
            ["model", "User"],
            ["model", "Item"],
            ["function", "authenticate"],
            ["function", "create_item"],
          ),
        ],
      ),
      outside("outside:postgresql", "PostgreSQL", "database"),
      outside("outside:email-smtp", "Email (SMTP)", "email"),
      outside("outside:sentry", "Sentry", "monitoring"),
    ],
    edges: [
      {
        from: "actor:user",
        to: "part:web-app",
        type: null,
        label: "uses",
        verified: false,
        confidence: null,
        provenance: null,
        inferred: { reason: "people use the app through its pages" },
      },
      line("part:web-app", "part:api-client", "calls", "calls the backend", 7, [
        ["useAuth", "LoginService.loginAccessToken", "frontend/src/hooks/useAuth.ts", 52],
        ["AddItem", "ItemsService.createItem", "frontend/src/components/Items/AddItem.tsx", 61],
      ]),
      line("part:api-client", "part:rest-api", "targets", "sends HTTP requests", 18, [
        [
          "LoginService.loginAccessToken",
          "POST /api/v1/login/access-token",
          "frontend/src/client/sdk.gen.ts",
          31,
        ],
        ["ItemsService.createItem", "POST /api/v1/items/", "frontend/src/client/sdk.gen.ts", 88],
      ]),
      line("part:rest-api", "part:data-models", "calls", "reads and saves items", 14, [
        ["login_access_token", "authenticate", "backend/app/api/routes/login.py", 31],
        ["create_item", "Item", "backend/app/api/routes/items.py", 77],
      ]),
      line("part:rest-api", "part:auth-security", "injects", "checks the session", 7, [
        ["read_items", "get_current_user", "backend/app/api/routes/items.py", 22],
      ]),
      line("part:auth-security", "part:data-models", "reads", "loads the user", 3, [
        ["get_current_user", "User", "backend/app/api/deps.py", 43],
      ]),
      line("part:data-models", "outside:postgresql", "reads", "stores users and items", 11),
      line("part:auth-security", "outside:email-smtp", "imports", "sends emails", 1),
      line("part:rest-api", "outside:sentry", "imports", "reports errors", 1),
    ],
  };
}

const column = (name: string, type: string, extra: Partial<SpecMember> = {}): SpecMember => ({
  name,
  kind: "column",
  type,
  ...extra,
});

function table(id: string, label: string, members: SpecMember[], line: number): SpecNode {
  return {
    id,
    label,
    layer: "db",
    kind: "table",
    citation: { path: "backend/app/models.py", start_line: line, end_line: line + 12 },
    members,
  };
}

/** The template's schema, as the ER diagram draws it. */
export function appSchema(title: string): Spec {
  return {
    title,
    kind: "er",
    nodes: [
      table(
        "table item",
        "item",
        [
          { name: "id", kind: "pk", type: "UUID", nullable: false },
          column("title", "str", { nullable: false }),
          column("description", "str", { nullable: true }),
          column("created_at", "datetime", { nullable: true }),
          { name: "owner_id", kind: "fk", type: "UUID", ref: "user.id", nullable: false },
        ],
        93,
      ),
      table(
        "table user",
        "user",
        [
          { name: "id", kind: "pk", type: "UUID", nullable: false },
          column("email", "EmailStr", { nullable: false }),
          column("is_active", "bool", { nullable: false }),
          column("is_superuser", "bool", { nullable: false }),
          column("full_name", "str", { nullable: true }),
          column("hashed_password", "str", { nullable: false }),
          column("created_at", "datetime", { nullable: true }),
        ],
        44,
      ),
    ],
    edges: [
      {
        from: "table item",
        to: "table user",
        type: "many_to_one",
        label: "owner_id → id",
        verified: true,
        confidence: 1,
        provenance: "contract",
        inferred: null,
      },
    ],
  };
}
