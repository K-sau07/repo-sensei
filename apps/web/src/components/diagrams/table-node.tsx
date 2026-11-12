"use client";

/**
 * An ER diagram's pieces (6.6): a card per table, its name at its head, a row per column (its
 * key marked with a key, a foreign key with a link and the column it names, its type, and "?"
 * where it may be null); and the crow's-foot ends its relations are drawn with: many (or one)
 * rows of the table holding the key, to one of the table it names (or none, where the key may
 * be null; er-markers.tsx). Each relation leaves from its key's row and reaches the named
 * column's row (the node and its handles: diagram-nodes.tsx).
 */
import { cn } from "cn";
import { KeyRound, Link2, Table2 } from "lucide-react";

import { citationLabel, type SpecMember, type SpecNode } from "@/lib/api/diagrams";
import { COLUMN_ROW, TABLE_HEAD } from "@/lib/diagrams/er-layout";

import { laneOf } from "./blueprint-node";

export function TableCard({
  node,
  added = false,
  selected = false,
}: {
  node: SpecNode;
  added?: boolean;
  selected?: boolean;
}) {
  const color = laneOf("db").color;
  const members = node.members ?? [];
  const more = node.more_members ?? 0;
  const cited = citationLabel(node.citation);
  return (
    <div
      className={cn(
        "flex h-full w-full cursor-pointer flex-col overflow-hidden rounded-lg border bg-surface-1 shadow-sm",
        "border-line-strong hover:border-fg-muted",
        selected && "border-fg ring-2 ring-fg/20",
        added && "ring-2 ring-ok/60",
      )}
      data-testid="table-node"
    >
      <div
        className="flex items-center gap-2 border-b border-line px-3"
        style={{
          height: TABLE_HEAD,
          background: `color-mix(in oklch, ${color} 14%, var(--surface-1))`,
        }}
      >
        <Table2 aria-hidden className="size-4 shrink-0" style={{ color }} />
        <span className="min-w-0 truncate text-sm font-semibold text-fg" title={node.id}>
          {node.label}
        </span>
        <span className="ml-auto shrink-0 truncate font-mono text-[10px] text-fg-faint">
          {node.kind === "collection" ? "collection" : (cited ?? "table")}
        </span>
      </div>
      {members.length > 0 ? (
        <ul aria-label={`${node.label}'s columns`}>
          {members.map((member) => (
            <ColumnRow key={member.name} member={member} />
          ))}
          {more > 0 ? (
            <li
              className="px-3 text-[11px] text-fg-faint"
              style={{ height: COLUMN_ROW, lineHeight: `${String(COLUMN_ROW)}px` }}
            >
              +{more} more
            </li>
          ) : null}
        </ul>
      ) : (
        <p
          className="px-3 text-[11px] text-fg-faint"
          style={{ lineHeight: `${String(COLUMN_ROW)}px` }}
        >
          No columns read
        </p>
      )}
    </div>
  );
}

function ColumnRow({ member }: { member: SpecMember }) {
  const key = member.kind === "pk";
  const foreign = Boolean(member.ref);
  return (
    <li
      className="flex items-center gap-2 border-b border-line/50 px-3 font-mono text-[11px] last:border-b-0"
      style={{ height: COLUMN_ROW }}
    >
      <span aria-hidden className="flex w-3.5 shrink-0 justify-center">
        {key ? (
          <KeyRound className="size-3 text-warn" />
        ) : foreign ? (
          <Link2 className="size-3 text-layer-api" />
        ) : null}
      </span>
      {key ? <span className="sr-only">Primary key </span> : null}
      <span className={cn("min-w-0 truncate", key ? "font-semibold text-fg" : "text-fg")}>
        {member.name}
      </span>
      {foreign ? (
        <span className="min-w-0 truncate text-[10px] text-fg-faint">
          <span className="sr-only">, a foreign key to </span>
          <span aria-hidden>→ </span>
          {member.ref}
        </span>
      ) : null}
      <span className="ml-auto shrink-0 text-fg-muted">
        {member.type ?? ""}
        {member.nullable ? (
          <>
            <span aria-hidden className="text-fg-faint">
              ?
            </span>
            <span className="sr-only">, may be null</span>
          </>
        ) : null}
      </span>
    </li>
  );
}
