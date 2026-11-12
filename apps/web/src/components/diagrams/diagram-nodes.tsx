"use client";

/**
 * The architecture's and the ER diagram's nodes on the canvas (6.6): each node's box and its
 * handles, drawn at once, its card (with its icons) loaded with its view, so no flowchart pays
 * for them. The handles are there from the first frame: React Flow finds every line's ends when
 * it first measures the node, whatever the card is still loading.
 *
 * An architecture box's lines leave at its bottom and reach the next row at its top; within a
 * row, from one box's side to the other's; past a row, round the side. A table's relation leaves
 * from its key's row and reaches the row of the column it names (or the table's head).
 */
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { cn } from "cn";
import { lazy, Suspense } from "react";

import type { SpecNode } from "@/lib/api/diagrams";
import { COLUMN_ROW, TABLE_HEAD, TABLE_WIDTH, tableHeight } from "@/lib/diagrams/er-layout";
import { sizeOf, type Row } from "@/lib/diagrams/system-layout";

import { laneOf } from "./blueprint-node";

const SystemCard = lazy(() => import("./system-node").then((m) => ({ default: m.SystemCard })));
const TableCard = lazy(() => import("./table-node").then((m) => ({ default: m.TableCard })));

const HIDDEN = "opacity-0";

export interface SystemData extends Record<string, unknown> {
  node: SpecNode;
  added: boolean;
}

export function SystemNode({ data, selected }: NodeProps<Node<SystemData>>) {
  const { node, added } = data;
  const size = sizeOf(node);
  return (
    <div style={{ width: size.width, height: size.height }}>
      <Handle type="target" id="top" position={Position.Top} className={HIDDEN} />
      <Handle type="target" id="left" position={Position.Left} className={HIDDEN} />
      <Handle type="target" id="right" position={Position.Right} className={HIDDEN} />
      <Suspense fallback={<div className="h-full w-full rounded-xl border border-line" />}>
        <SystemCard node={node} added={added} selected={selected} />
      </Suspense>
      <Handle type="source" id="bottom" position={Position.Bottom} className={HIDDEN} />
      <Handle type="source" id="to-left" position={Position.Left} className={HIDDEN} />
      <Handle type="source" id="to-right" position={Position.Right} className={HIDDEN} />
      <Handle type="source" id="up" position={Position.Top} className={HIDDEN} />
    </div>
  );
}

export interface RowLabelData extends Record<string, unknown> {
  row: Row;
  height: number;
}

/** A row's name, faint, at its left (who uses it, the frontend, the API...). */
export function RowLabelNode({ data }: NodeProps<Node<RowLabelData>>) {
  const lane = laneOf(data.row.lane);
  return (
    <div
      className={cn(
        "flex items-center justify-end pr-2 text-[11px] font-medium tracking-wide uppercase opacity-70",
        lane.text,
      )}
      style={{ width: 120, height: data.height }}
      data-testid="system-row"
    >
      {data.row.label}
    </div>
  );
}

export interface TableData extends Record<string, unknown> {
  node: SpecNode;
  added: boolean;
}

export function TableNode({ data, selected }: NodeProps<Node<TableData>>) {
  const { node, added } = data;
  const members = node.members ?? [];
  const rowY = (i: number) => TABLE_HEAD + i * COLUMN_ROW + COLUMN_ROW / 2;
  return (
    <div style={{ width: TABLE_WIDTH, height: tableHeight(node) }}>
      <Handle
        type="target"
        id="in:head"
        position={Position.Left}
        style={{ top: TABLE_HEAD / 2 }}
        className={HIDDEN}
      />
      {members.map((member, i) => (
        <span key={member.name}>
          <Handle
            type="target"
            id={`in:${member.name}`}
            position={Position.Left}
            style={{ top: rowY(i) }}
            className={HIDDEN}
          />
          <Handle
            type="source"
            id={`out:${member.name}`}
            position={Position.Right}
            style={{ top: rowY(i) }}
            className={HIDDEN}
          />
        </span>
      ))}
      <Suspense fallback={<div className="h-full w-full rounded-lg border border-line" />}>
        <TableCard node={node} added={added} selected={selected} />
      </Suspense>
    </div>
  );
}
