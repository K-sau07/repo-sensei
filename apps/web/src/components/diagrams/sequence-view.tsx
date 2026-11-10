"use client";

/**
 * The sequence view (6.6.2): who takes part, left to right in the order they join (each a card:
 * a click opens its code), a lifeline down from each, and the messages between them, top to
 * bottom in the flow's order, numbered, with what each does. A message the graph proves is solid;
 * one it doesn't is dashed amber, saying why it is inferred. A wide one is shown fitted to the
 * canvas (down to MIN_FIT of its size), or at its actual size, scrolled.
 */
import { cn } from "cn";
import { Maximize2, Minimize2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import type { Spec, SpecEdge, SpecNode } from "@/lib/api/diagrams";
import { edgeWords } from "@/lib/api/diagrams";
import { CARD_HEIGHT, CARD_WIDTH, LOOP, sequenceOf, type Message } from "@/lib/diagrams/sequence";

import { BlueprintCard } from "./blueprint-node";

const VERIFIED = "var(--text-muted)";
const INFERRED = "var(--warn)";
const MIN_FIT = 0.55; // smaller than this and its words can't be read: scroll instead
const PAD = 48; // the view's padding, both sides

export function SequenceView({
  spec,
  edges,
  selected,
  onSelect,
  added,
}: {
  spec: Spec;
  edges: readonly SpecEdge[];
  selected: string | null;
  onSelect: (node: SpecNode | null) => void;
  added?: ReadonlySet<string> | undefined;
}) {
  const sequence = sequenceOf(spec, edges);
  const box = useRef<HTMLDivElement>(null);
  const [room, setRoom] = useState<number | null>(null);
  const [actual, setActual] = useState(false);
  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setRoom(entry.contentRect.width);
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, []);
  const fit = room === null ? 1 : Math.max(MIN_FIT, Math.min(1, (room - PAD) / sequence.width));
  const scale = actual ? 1 : fit;
  return (
    <div
      ref={box}
      className="relative h-full min-h-[28rem] w-full overflow-auto bg-[radial-gradient(var(--line-strong)_1px,transparent_1px)] [background-size:18px_18px] p-6"
      data-testid="sequence-view"
    >
      {fit < 1 ? (
        <Button
          size="sm"
          variant="ghost"
          className="sticky top-0 left-0 z-10 mb-2 bg-surface-1/80 backdrop-blur"
          aria-pressed={actual}
          onClick={() => {
            setActual((was) => !was);
          }}
        >
          {actual ? <Minimize2 aria-hidden /> : <Maximize2 aria-hidden />}
          {actual ? "Fit to view" : "Actual size"}
        </Button>
      ) : null}
      <div
        className="mx-auto"
        style={{ width: sequence.width * scale, height: sequence.height * scale }}
      >
        <div
          className="relative origin-top-left"
          style={{
            width: sequence.width,
            height: sequence.height,
            transform: scale === 1 ? undefined : `scale(${String(scale)})`,
          }}
        >
          <svg
            className="absolute inset-0"
            width={sequence.width}
            height={sequence.height}
            role="img"
            aria-label={`Sequence: ${String(sequence.messages.length)} messages between ${String(sequence.participants.length)} participants`}
          >
            <defs>
              {[
                ["verified", VERIFIED],
                ["inferred", INFERRED],
              ].map(([id, color]) => (
                <marker
                  key={id}
                  id={`seq-${id ?? ""}`}
                  viewBox="0 0 10 10"
                  refX="10"
                  refY="5"
                  markerWidth="7"
                  markerHeight="7"
                  orient="auto-start-reverse"
                >
                  <path d="M0,0 L10,5 L0,10 z" fill={color} />
                </marker>
              ))}
            </defs>
            {sequence.participants.map((p) => (
              <line
                key={p.node.id}
                x1={p.x}
                x2={p.x}
                y1={CARD_HEIGHT}
                y2={sequence.height}
                stroke="var(--line-strong)"
                strokeDasharray="4 4"
              />
            ))}
            {sequence.messages.map((message) => (
              <Arrow key={message.n} message={message} />
            ))}
          </svg>
          {sequence.participants.map((p) => (
            <button
              key={p.node.id}
              type="button"
              className="absolute top-0 text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
              style={{
                left: p.x - CARD_WIDTH / 2,
                width: CARD_WIDTH,
                height: CARD_HEIGHT,
              }}
              aria-pressed={selected === p.node.id}
              title="Open its code"
              onClick={() => {
                onSelect(selected === p.node.id ? null : p.node);
              }}
            >
              <BlueprintCard
                node={p.node}
                added={added?.has(p.node.id) ?? false}
                selected={selected === p.node.id}
              />
            </button>
          ))}
        </div>
      </div>
      <p className="sr-only">
        {sequence.messages
          .map((m) => `${String(m.n)}: ${edgeWords(m.edge)}${m.edge.verified ? "" : " (inferred)"}`)
          .join("; ")}
      </p>
    </div>
  );
}

function Arrow({ message }: { message: Message }) {
  const { edge, from, to, y, n, self } = message;
  const color = edge.verified ? VERIFIED : INFERRED;
  const dash = edge.verified ? undefined : "6 4";
  const marker = `url(#seq-${edge.verified ? "verified" : "inferred"})`;
  const words = edgeWords(edge);
  const why = edge.verified ? undefined : edge.inferred?.reason;
  const left = Math.min(from, to);
  const mid = self ? from + LOOP + 8 : (from + to) / 2;
  return (
    <g className={cn(edge.verified ? "verified" : "inferred")} data-testid="sequence-message">
      {why ? <title>{`Inferred: ${why}`}</title> : null}
      {self ? (
        <path
          d={`M${String(from)},${String(y - 8)} h${String(LOOP)} v16 h-${String(LOOP)}`}
          fill="none"
          stroke={color}
          strokeWidth={1.5}
          strokeDasharray={dash}
          markerEnd={marker}
        />
      ) : (
        <line
          x1={from}
          x2={to}
          y1={y}
          y2={y}
          stroke={color}
          strokeWidth={1.5}
          strokeDasharray={dash}
          markerEnd={marker}
        />
      )}
      <circle cx={self ? from : left + 10} cy={y - 12} r={8} fill="var(--surface-3)" />
      <text
        x={self ? from : left + 10}
        y={y - 8.5}
        textAnchor="middle"
        fontSize={10}
        fontWeight={600}
        fill="var(--text)"
      >
        {n}
      </text>
      <text x={mid} y={y - 7} textAnchor="middle" fontSize={11} fill="var(--text-muted)">
        {words.length > 34 ? `${words.slice(0, 33)}…` : words}
      </text>
    </g>
  );
}
