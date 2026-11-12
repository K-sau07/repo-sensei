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

function line(from: string, to: string, type: string, label: string): SpecEdge {
  return {
    from,
    to,
    type,
    label,
    verified: true,
    confidence: 0.9,
    provenance: "graph",
    inferred: null,
  };
}

export function appArchitecture(title: string): Spec {
  return {
    title,
    kind: "architecture",
    summary:
      "A full-stack items app: a React frontend calls a FastAPI backend that signs users in and keeps their items in PostgreSQL.",
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
        folders(ROUTES, ITEMS_UI, HOOKS),
      ),
      part(
        "part:api-client",
        "API client",
        "frontend",
        "plug",
        "Generated services the pages call the backend through.",
        folders(CLIENT),
      ),
      part(
        "part:rest-api",
        "REST API",
        "api",
        "server",
        "Endpoints for login, users, items and password recovery.",
        folders(API),
      ),
      part(
        "part:auth-security",
        "Auth & security",
        "service",
        "shield",
        "Settings, password hashing and access tokens.",
        folders(CORE),
      ),
      part(
        "part:data-models",
        "Data models",
        "data",
        "database",
        "User and Item models and the code that reads and writes them.",
        folders(APP),
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
      line("part:web-app", "part:api-client", "calls", "7 calls"),
      line("part:api-client", "part:rest-api", "targets", "18 HTTP requests"),
      line("part:rest-api", "part:data-models", "calls", "14 calls"),
      line("part:rest-api", "part:auth-security", "injects", "7 injects"),
      line("part:auth-security", "part:data-models", "reads", "3 reads"),
      line("part:data-models", "outside:postgresql", "reads", "11 reads and writes"),
      line("part:auth-security", "outside:email-smtp", "imports", "imports"),
      line("part:rest-api", "outside:sentry", "imports", "imports"),
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
