/**
 * repo-sensei's mark and name. The mark is an S drawn as a Trace Beam: one path through three
 * hops, from the UI (blue) through the code (the signal's green) to the data (violet), on a dark
 * tile. Its colors are the brand's, fixed: they don't follow the theme or a chosen accent.
 * The same drawing is the favicon (app/icon.svg).
 */
import { cn } from "cn";
import Link from "next/link";

// Fixed ids (a server component has no useId): every copy on a page defines the same gradients,
// so whichever one an id resolves to, it is the same drawing.
const beam = "sensei-mark-beam";
const glow = "sensei-mark-glow";

export function LogoMark({ className, title }: { className?: string; title?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      className={cn("size-7 shrink-0", className)}
      {...(title ? { role: "img", "aria-label": title } : { "aria-hidden": true })}
    >
      <defs>
        <linearGradient id={beam} x1="24" y1="7" x2="8" y2="25" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#8fc6ff" />
          <stop offset="0.5" stopColor="#42e9bc" />
          <stop offset="1" stopColor="#d9abfb" />
        </linearGradient>
        <radialGradient id={glow} cx="16" cy="16" r="16" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#42e9bc" stopOpacity="0.22" />
          <stop offset="1" stopColor="#42e9bc" stopOpacity="0" />
        </radialGradient>
      </defs>
      <rect
        x="0.5"
        y="0.5"
        width="31"
        height="31"
        rx="8"
        fill="#141619"
        stroke="#ffffff"
        strokeOpacity="0.12"
      />
      <rect x="0.5" y="0.5" width="31" height="31" rx="8" fill={`url(#${glow})`} />
      <path
        d="M22.4 8.4C17.2 6.2 9.8 8 10.6 12.5c.8 4.3 10.4 3.4 11 7.6.6 4.2-6.6 6.2-11.9 3.5"
        fill="none"
        stroke={`url(#${beam})`}
        strokeWidth="2.6"
        strokeLinecap="round"
      />
      <circle cx="22.4" cy="8.4" r="2.4" fill="#8fc6ff" />
      <circle cx="16.1" cy="16.1" r="2.1" fill="#141619" stroke="#42e9bc" strokeWidth="1.6" />
      <circle cx="9.7" cy="23.6" r="2.4" fill="#d9abfb" />
    </svg>
  );
}

/** The name, set as the brand: "repo" quiet, "sensei" bright. */
export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cn("text-[17px] font-medium tracking-[-0.02em] text-fg-muted", className)}>
      repo<span className="text-fg-faint">-</span>
      <span className="font-semibold text-fg">sensei</span>
    </span>
  );
}

/** The mark and the name, home. */
export function Brand({ className, href = "/" }: { className?: string; href?: "/" }) {
  return (
    <Link
      href={href}
      aria-label="repo-sensei, home"
      className={cn("flex items-center gap-2.5 rounded-md", className)}
    >
      <LogoMark />
      <Wordmark />
    </Link>
  );
}
