"use client";

/** Cards lit where the pointer is (the landing page's features): one listener for the grid sets
 * each card's light to the pointer's place in it. Decoration only. */
export function Spotlight({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={className}
      onPointerMove={(event) => {
        for (const card of event.currentTarget.querySelectorAll<HTMLElement>("[data-spotlight]")) {
          const box = card.getBoundingClientRect();
          card.style.setProperty("--spot-x", `${String(event.clientX - box.left)}px`);
          card.style.setProperty("--spot-y", `${String(event.clientY - box.top)}px`);
        }
      }}
    >
      {children}
    </div>
  );
}
