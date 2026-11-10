"use client";

/**
 * The architecture view's pieces (6.6.2), drawn as an entity diagram: a card per part of the
 * app (a page, a route, a service, a model, a table), its head saying its layer (glyph and name,
 * never color alone), what it is and where it is, its body listing its members (a table's
 * columns, a class's methods and fields, a component's hooks and what it renders) as rows; and
 * a band per tier (frontend, API, services, data), behind its cards, named.
 */
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { cn } from "cn";

import { citationLabel, type SpecMember, type SpecNode } from "@/lib/api/diagrams";
import { ENTITY_WIDTH, MEMBER_ROW, entityHeight, type Tier } from "@/lib/diagrams/elk-layout";

import { laneOf } from "./blueprint-node";

/** A member's mark and how it's said: `ƒ` a method, `#` a column, and so on. */
const MEMBER: Record<string, { mark: string; said: string }> = {
  method: { mark: "ƒ", said: "method" },
  function: { mark: "ƒ", said: "function" },
  variable: { mark: "·", said: "field" },
  class: { mark: "◇", said: "class" },
  column: { mark: "#", said: "column" },
  hook: { mark: "⚓", said: "hook" },
  renders: { mark: "▸", said: "renders" },
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
};

export function kindWords(kind: string): string {
  return KIND_WORDS[kind] ?? kind.replaceAll("_", " ");
}

export interface EntityData extends Record<string, unknown> {
  node: SpecNode;
  added: boolean;
}

export function EntityCard({
  node,
  added = false,
  selected = false,
}: {
  node: SpecNode;
  added?: boolean;
  selected?: boolean;
}) {
  const lane = laneOf(node.layer);
  const cited = citationLabel(node.citation);
  const members = node.members ?? [];
  const more = node.more_members ?? 0;
  return (
    <div
      className={cn(
        "flex h-full w-full cursor-pointer flex-col overflow-hidden rounded-md border bg-surface-1 shadow-sm",
        "border-line-strong hover:border-fg-muted",
        selected && "border-fg ring-2 ring-fg/20",
        added && "ring-2 ring-ok/60",
      )}
      data-testid="entity-node"
    >
      <div
        className="flex flex-col gap-0.5 border-b border-line px-3 py-2"
        style={{ borderTop: `3px solid ${lane.color}` }}
      >
        <span className="flex items-center gap-1.5 text-[10px] tracking-wide uppercase">
          <span aria-hidden className={lane.text}>
            {lane.glyph}
          </span>
          <span className={lane.text}>{lane.label}</span>
          <span className="text-fg-faint normal-case">· {kindWords(node.kind)}</span>
          {added ? <span className="ml-auto text-ok normal-case">new</span> : null}
        </span>
        <span className="truncate text-sm font-semibold text-fg" title={node.id}>
          {node.label}
        </span>
        {cited ? (
          <span className="truncate font-mono text-[10px] text-fg-muted">{cited}</span>
        ) : null}
      </div>
      {members.length > 0 ? (
        <ul className="flex flex-col py-1" aria-label={`${node.label}'s members`}>
          {members.map((member) => (
            <MemberRow key={`${member.kind}:${member.name}`} member={member} />
          ))}
          {more > 0 ? (
            <li
              className="px-3 text-[11px] text-fg-faint"
              style={{ height: MEMBER_ROW, lineHeight: `${String(MEMBER_ROW)}px` }}
            >
              +{more} more
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}

function MemberRow({ member }: { member: SpecMember }) {
  const shown = MEMBER[member.kind] ?? { mark: "·", said: member.kind };
  return (
    <li
      className="flex items-center gap-2 px-3 font-mono text-[11px] text-fg-muted"
      style={{ height: MEMBER_ROW }}
    >
      <span aria-hidden className="w-3 text-center text-fg-faint">
        {shown.mark}
      </span>
      <span className="sr-only">{shown.said}</span>
      <span className="truncate">{member.name}</span>
    </li>
  );
}

export function EntityNode({ data, selected }: NodeProps<Node<EntityData>>) {
  const { node, added } = data;
  return (
    <div style={{ width: ENTITY_WIDTH, height: entityHeight(node) }}>
      <Handle type="target" position={Position.Left} className="opacity-0" />
      <EntityCard node={node} added={added} selected={selected} />
      <Handle type="source" position={Position.Right} className="opacity-0" />
    </div>
  );
}

export interface BandData extends Record<string, unknown> {
  tier: Tier;
  width: number;
  height: number;
}

/** A tier's band (frontend, API, services, data): named at its top, faintly in its color,
 * behind its cards. */
export function BandNode({ data }: NodeProps<Node<BandData>>) {
  const lane = laneOf(data.tier.lane);
  return (
    <div
      className="rounded-xl border border-dashed"
      style={{
        width: data.width,
        height: data.height,
        borderColor: `color-mix(in oklch, ${lane.color} 45%, transparent)`,
        background: `color-mix(in oklch, ${lane.color} 6%, transparent)`,
      }}
      data-testid="layer-band"
    >
      <span
        className={cn(
          "flex items-center gap-1.5 px-4 pt-3 text-xs font-medium tracking-wide uppercase",
          lane.text,
        )}
      >
        <span aria-hidden>{lane.glyph}</span>
        {data.tier.label}
      </span>
    </div>
  );
}
