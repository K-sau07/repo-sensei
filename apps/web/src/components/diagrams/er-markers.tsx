/**
 * An ER diagram's relation ends (6.6), crow's feet: many (or one) rows of the table holding the
 * key, to one of the table it names (or none, where the key may be null). Kept apart from the
 * tables' cards, which load with their view.
 */
export const RELATION = "var(--text-muted)";

/** The crow's-foot ends, as SVG markers the relations name (drawn as an end, toward the
 * table; a line's start is turned round). */
export function RelationMarkers() {
  const line = { fill: "none", stroke: RELATION, strokeWidth: 1.5 } as const;
  const marker = (id: string, body: React.ReactNode) => (
    <marker
      id={id}
      viewBox="0 0 20 20"
      refX="20"
      refY="10"
      markerWidth="20"
      markerHeight="20"
      markerUnits="userSpaceOnUse"
      orient="auto-start-reverse"
    >
      {body}
    </marker>
  );
  return (
    <svg aria-hidden className="absolute h-0 w-0">
      <defs>
        {marker("er-one", <path d="M12,4 V16 M16,4 V16" {...line} />)}
        {marker(
          "er-zero-one",
          <>
            <path d="M16,4 V16" {...line} />
            <circle cx="9" cy="10" r="3.5" fill="var(--surface-1)" stroke={RELATION} />
          </>,
        )}
        {marker(
          "er-zero-many",
          <>
            <path d="M20,3 L12,10 L20,17 M12,10 H20" {...line} />
            <circle cx="7" cy="10" r="3.5" fill="var(--surface-1)" stroke={RELATION} />
          </>,
        )}
      </defs>
    </svg>
  );
}

/** A relation's ends: zero or many (or zero or one, for a one-to-one) rows of the table holding
 * the key, to exactly one row of the one it names, or none when the key may be null. */
export function relationMarkers(type: string | null, nullable: boolean) {
  return {
    markerStart: type === "one_to_one" ? "er-zero-one" : "er-zero-many",
    markerEnd: nullable ? "er-zero-one" : "er-one",
  };
}

/** How a relation is said aloud: "Many items to one user". */
export function relationWords(type: string | null): string {
  return type === "one_to_one" ? "one to one" : "many to one";
}
