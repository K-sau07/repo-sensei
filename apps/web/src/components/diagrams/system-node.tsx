"use client";

/**
 * The architecture view's boxes (6.6), drawn as a system diagram is on a whiteboard: each part a
 * rounded box tinted in its row's color, its icon, its name and what it does (a flow's code
 * node: what it is and where); a database, cache or table a cylinder; who uses the system a box
 * of its own at the top. The node around it, with its handles, is diagram-nodes.tsx's.
 */
import { cn } from "cn";
import {
  Activity,
  BarChart3,
  Bell,
  Bot,
  Box,
  Brain,
  Clock,
  Cloud,
  Cog,
  CreditCard,
  Database,
  FileText,
  Globe,
  HardDrive,
  Image,
  KeyRound,
  LayoutDashboard,
  Layers,
  ListOrdered,
  Lock,
  Mail,
  MessageSquare,
  Monitor,
  Package,
  Plug,
  Search,
  Server,
  Shield,
  ShieldCheck,
  ShoppingCart,
  Smartphone,
  Sparkles,
  Table2,
  Terminal,
  User,
  Users,
  Workflow,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { createElement } from "react";

import { citationLabel, type SpecNode } from "@/lib/api/diagrams";
import { isStore } from "@/lib/diagrams/system-layout";

import { colorOf } from "./system-color";

/** The icons a whole app's parts may name (the API's list). */
const ICONS: Record<string, LucideIcon> = {
  monitor: Monitor,
  layout: LayoutDashboard,
  smartphone: Smartphone,
  globe: Globe,
  server: Server,
  plug: Plug,
  shield: Shield,
  lock: Lock,
  key: KeyRound,
  users: Users,
  user: User,
  cog: Cog,
  workflow: Workflow,
  "credit-card": CreditCard,
  "shopping-cart": ShoppingCart,
  package: Package,
  mail: Mail,
  bell: Bell,
  message: MessageSquare,
  search: Search,
  file: FileText,
  image: Image,
  chart: BarChart3,
  clock: Clock,
  bot: Bot,
  brain: Brain,
  database: Database,
  box: Box,
  wrench: Wrench,
  terminal: Terminal,
};

/** An outside system's icon, by what it is. */
const OUTSIDE: Record<string, LucideIcon> = {
  database: Database,
  cache: Zap,
  search: Search,
  storage: HardDrive,
  queue: ListOrdered,
  email: Mail,
  payments: CreditCard,
  ai: Sparkles,
  monitoring: Activity,
  auth: ShieldCheck,
  messaging: MessageSquare,
  cloud: Cloud,
};

/** A flow's code node's icon, by its layer (a table's, by its kind). */
const LAYER: Record<string, LucideIcon> = {
  ui: Monitor,
  state: Layers,
  client: Plug,
  route: Globe,
  handler: Server,
  service: Cog,
  db: Database,
  other: Box,
};

/** What a part is, said as a reader would: `api_endpoint` is "endpoint". */
const KIND_WORDS: Record<string, string> = {
  api_endpoint: "endpoint",
  http_client_call: "request",
  orm_model: "model",
  graphql_operation: "GraphQL operation",
  graphql_field: "GraphQL field",
  rpc_method: "RPC method",
  sql_statement: "SQL",
  external_symbol: "external",
  pk: "primary key",
  fk: "foreign key",
};

export function kindWords(kind: string): string {
  return KIND_WORDS[kind] ?? kind.replaceAll("_", " ");
}

/** What an outside system is, said: "payments", "AI model", "database". */
const OUTSIDE_WORDS: Record<string, string> = {
  ai: "AI model",
  queue: "queue",
  cache: "cache",
  search: "search index",
  storage: "file storage",
  email: "email",
  payments: "payments",
  monitoring: "monitoring",
  auth: "sign-in",
  messaging: "messaging",
  cloud: "cloud",
  database: "database",
};

export function iconOf(node: SpecNode): LucideIcon {
  if (node.layer === "outside") return OUTSIDE[node.kind] ?? Cloud;
  if (node.layer === "actor") return User;
  if (node.icon) return ICONS[node.icon] ?? Box;
  if (node.kind === "table" || node.kind === "orm_model") return Table2;
  return LAYER[node.layer] ?? Box;
}

/** The line under a box's name: what the part does, else what the code node is and where. */
function subtitle(node: SpecNode): string | null {
  if (node.note) return node.note;
  if (node.layer === "outside") return OUTSIDE_WORDS[node.kind] ?? "outside service";
  if (node.layer === "actor") return null;
  const cited = citationLabel(node.citation);
  const kind = kindWords(node.kind);
  return cited ? `${kind} · ${cited}` : kind;
}

export function SystemCard({
  node,
  added = false,
  selected = false,
}: {
  node: SpecNode;
  added?: boolean;
  selected?: boolean;
}) {
  const color = colorOf(node);
  // The icon is one of the module's own components, picked by the node (not made here).
  const icon = (className: string) =>
    createElement(iconOf(node), { "aria-hidden": true, className, style: { color } });
  const said = subtitle(node);
  if (isStore(node)) {
    return (
      <div
        className={cn(
          "relative flex h-full w-full cursor-pointer flex-col items-center justify-center gap-0.5 px-4 pt-3",
          selected && "drop-shadow-[0_0_6px_var(--text-muted)]",
        )}
        data-testid="system-node"
        data-store="true"
      >
        <svg
          aria-hidden
          className="absolute inset-0 h-full w-full overflow-visible"
          viewBox="0 0 168 96"
          preserveAspectRatio="none"
        >
          <path
            d="M1,14 C1,6 167,6 167,14 L167,82 C167,90 1,90 1,82 Z"
            fill={`color-mix(in oklch, ${color} 14%, var(--surface-1))`}
            stroke={`color-mix(in oklch, ${color} 60%, transparent)`}
            strokeWidth={selected ? 2.5 : 1.5}
          />
          <path
            d="M1,14 C1,22 167,22 167,14"
            fill="none"
            stroke={`color-mix(in oklch, ${color} 60%, transparent)`}
            strokeWidth={selected ? 2.5 : 1.5}
          />
        </svg>
        {icon("relative size-4")}
        <span
          className="relative max-w-full truncate text-sm font-semibold text-fg"
          title={node.id}
        >
          {node.label}
        </span>
        {said ? (
          <span className="relative max-w-full truncate text-[10px] text-fg-muted">{said}</span>
        ) : null}
        {added ? <span className="relative text-[10px] text-ok">new</span> : null}
      </div>
    );
  }
  return (
    <div
      className={cn(
        "flex h-full w-full cursor-pointer items-center gap-3 rounded-xl border px-3 shadow-sm",
        "hover:shadow-md",
        selected && "ring-2 ring-fg/30",
        added && "ring-2 ring-ok/60",
      )}
      style={{
        background: `color-mix(in oklch, ${color} 10%, var(--surface-1))`,
        borderColor: `color-mix(in oklch, ${color} 50%, transparent)`,
      }}
      data-testid="system-node"
    >
      <span
        aria-hidden
        className="flex size-9 shrink-0 items-center justify-center rounded-lg"
        style={{ background: `color-mix(in oklch, ${color} 22%, transparent)`, color }}
      >
        {icon("size-5")}
      </span>
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="truncate text-sm font-semibold text-fg" title={node.id}>
          {node.label}
        </span>
        {said ? (
          <span className="line-clamp-2 text-[11px] leading-snug text-fg-muted">{said}</span>
        ) : null}
      </span>
      {added ? <span className="ml-auto self-start pt-2 text-[10px] text-ok">new</span> : null}
    </div>
  );
}
