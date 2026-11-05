"use client";

/** A section that rises into place as it scrolls into view (once). With reduced motion it only
 * fades (the app's MotionConfig). It starts hidden, so `data-reveal` shows it where nothing will
 * animate it: without JavaScript (the marketing layout's noscript style) and in print. */
import * as m from "motion/react-m";

export function Reveal({
  children,
  className,
  delay = 0,
}: {
  children: React.ReactNode;
  className?: string;
  delay?: number;
}) {
  return (
    <m.div
      data-reveal
      className={className}
      initial={{ opacity: 0, y: 24 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "0px 0px -12% 0px" }}
      transition={{ duration: 0.6, delay, ease: [0.22, 1, 0.36, 1] }}
    >
      {children}
    </m.div>
  );
}
