"use client";

/**
 * The landing page's scroll story (10.3, GSAP ScrollTrigger: the one place it is used), played
 * by the scroll as it comes into view (never pinned: the page never stops). A question is asked; the hops of the real sign-in trace
 * drop into their layers; the Trace Beam runs through them; each names its file and line and how
 * sure it is. Drawn whole on the server and with reduced motion (the timeline only starts from
 * an empty stage when motion is allowed), so nothing depends on it running.
 */
import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { useRef } from "react";

import type { StoryHop } from "@/lib/demo/product-demo";

gsap.registerPlugin(useGSAP, ScrollTrigger);

const QUESTION = "What happens when someone signs in?";
const STEPS = [
  { title: "Ask in plain words", body: "A question about the code, as you would ask a colleague." },
  { title: "It finds the code in every layer", body: "The page, the API client, the route, its handler and the queries." },
  { title: "And follows the request through them", body: "Across languages: the frontend's call is matched to the route that serves it." },
  { title: "Each step names its lines", body: "Its file and line, and how sure it is: ● from the code itself, ◐ matched by a rule." },
] as const; // prettier-ignore

const BANDS = [
  { label: "UI", layers: ["ui", "state"], color: "var(--layer-ui)" },
  { label: "API", layers: ["client", "route"], color: "var(--layer-api)" },
  { label: "Service", layers: ["handler", "service"], color: "var(--layer-svc)" },
  { label: "Data", layers: ["db"], color: "var(--layer-db)" },
] as const;

const W = 1080;
const BAND_H = 92;
const TOP = 10;
const CARD_W = 104;
const CARD_H = 54;
const LEFT = 64;
const STEP_X = 112;
const H = TOP + BANDS.length * BAND_H + 6;

function bandOf(layer: string): number {
  const at = BANDS.findIndex((band) => (band.layers as readonly string[]).includes(layer));
  return at < 0 ? 2 : at;
}

const clip = (text: string, max = 10) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);

export function ScrollStory({ hops, repo }: { hops: readonly StoryHop[]; repo: string }) {
  const root = useRef<HTMLElement>(null);
  const cards = hops.map((hop, i) => {
    const band = bandOf(hop.layer);
    const x = LEFT + i * STEP_X;
    const y = TOP + band * BAND_H + (BAND_H - CARD_H) / 2 + 8;
    return { hop, x, y, cx: x + CARD_W / 2, cy: y + CARD_H / 2 };
  });
  let beam = "";
  cards.forEach((card, i) => {
    if (i === 0) {
      beam = `M${String(card.cx)} ${String(card.cy)}`;
      return;
    }
    const previous = cards[i - 1];
    if (!previous) return;
    const bend = (card.cx - previous.cx) / 2;
    beam += `C${String(previous.cx + bend)} ${String(previous.cy)} ${String(card.cx - bend)} ${String(card.cy)} ${String(card.cx)} ${String(card.cy)}`;
  });

  useGSAP(
    () => {
      const motion = gsap.matchMedia();
      motion.add("(prefers-reduced-motion: no-preference)", () => {
        if (document.documentElement.dataset.motion === "reduce") return;
        const section = root.current;
        if (!section) return;
        const timeline = gsap.timeline({
          defaults: { ease: "power2.out" },
          scrollTrigger: {
            // Played by the scroll as the story comes up the screen (never pinned: the page keeps
            // moving), done by the time it is in the middle.
            trigger: section,
            start: "top 85%",
            end: "center 55%",
            scrub: 0.6,
            onUpdate: (self) => {
              section.dataset.step = String(Math.min(4, Math.floor(self.progress * 4) + 1));
            },
          },
        });
        timeline
          .fromTo(section, { "--typed": 0 }, { "--typed": 1, duration: 1, ease: "none" })
          .from("[data-story=band]", { opacity: 0, x: -24, stagger: 0.08, duration: 0.4 }, 1)
          .from(
            "[data-story=card]",
            {
              opacity: 0,
              y: -26,
              scale: 0.9,
              stagger: 0.08,
              duration: 0.5,
              transformOrigin: "50% 50%",
            },
            1.1,
          ) // prettier-ignore
          .fromTo(
            "[data-story=beam]",
            { strokeDashoffset: 1 },
            { strokeDashoffset: 0, duration: 1.1, ease: "none" },
            2.1,
          ) // prettier-ignore
          .from("[data-story=cite]", { opacity: 0, y: 6, stagger: 0.07, duration: 0.35 }, 3.1)
          .to({}, { duration: 0.35 });
        // Where the pin starts and ends is measured from the page: measured again whenever the
        // page's height changes (the live demo above streams in after the first paint).
        let frame = 0;
        const remeasure = new ResizeObserver(() => {
          cancelAnimationFrame(frame);
          frame = requestAnimationFrame(() => {
            ScrollTrigger.refresh();
          });
        });
        remeasure.observe(document.body);
        return () => {
          cancelAnimationFrame(frame);
          remeasure.disconnect();
        };
      });
      return () => {
        motion.revert();
      };
    },
    { scope: root },
  );

  return (
    <section
      ref={root}
      aria-labelledby="story-title"
      data-step="4"
      className="story relative flex flex-col gap-8"
      style={{ "--typed": 1 } as React.CSSProperties}
    >
      <div className="flex flex-col gap-6">
        <div className="flex flex-col gap-5">
          <h2 id="story-title" className="text-2xl font-semibold tracking-tight sm:text-3xl">
            One question, through the whole stack
          </h2>
          <ol className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((step, i) => (
              <li key={step.title} data-index={i + 1} className="story-step flex gap-3">
                <span className="story-dot mt-1.5 size-2 shrink-0 rounded-full" aria-hidden />
                <span className="flex flex-col gap-0.5">
                  <span className="story-title font-medium">{step.title}</span>
                  <span className="text-sm text-fg-muted">{step.body}</span>
                </span>
              </li>
            ))}
          </ol>
        </div>
        <div className="flex min-w-0 flex-col gap-4 rounded-2xl border border-line bg-surface-1/80 p-4 backdrop-blur sm:p-6">
          <p className="flex items-center gap-2 rounded-lg border border-line bg-bg px-3 py-2.5 font-mono text-sm">
            <span aria-hidden className="text-signal">
              ›
            </span>
            <span className="story-typed">{QUESTION}</span>
          </p>
          <svg
            viewBox={`0 0 ${String(W)} ${String(H)}`}
            className="w-full"
            role="img"
            aria-label={`The sign-in trace of ${repo}: ${hops.map((h) => h.name).join(", then ")}.`}
          >
            <defs>
              <linearGradient id="story-beam" x1="0" x2="1" y1="0" y2="0">
                {BANDS.map((band, i) => (
                  <stop key={band.label} offset={i / (BANDS.length - 1)} stopColor={band.color} />
                ))}
              </linearGradient>
              <filter id="story-glow" x="-20%" y="-20%" width="140%" height="140%">
                <feGaussianBlur stdDeviation="4" result="blur" />
                <feMerge>
                  <feMergeNode in="blur" />
                  <feMergeNode in="SourceGraphic" />
                </feMerge>
              </filter>
            </defs>
            {BANDS.map((band, i) => (
              <g key={band.label} data-story="band">
                <rect
                  x={0}
                  y={TOP + i * BAND_H}
                  width={W}
                  height={BAND_H - 6}
                  rx={10}
                  fill={band.color}
                  opacity={0.06}
                />
                <text
                  x={12}
                  y={TOP + i * BAND_H + 18}
                  fill={band.color}
                  fontSize={13}
                  fontWeight={600}
                >
                  {band.label}
                </text>
              </g>
            ))}
            <path
              data-story="beam"
              d={beam}
              fill="none"
              stroke="url(#story-beam)"
              strokeWidth={3}
              pathLength={1}
              strokeDasharray="1"
              strokeDashoffset={0}
              filter="url(#story-glow)"
            />
            {cards.map(({ hop, x, y }, i) => (
              <g key={`${hop.name}-${String(i)}`} data-story="card">
                <rect
                  x={x}
                  y={y}
                  width={CARD_W}
                  height={CARD_H}
                  rx={8}
                  fill="var(--bg)"
                  stroke={BANDS[bandOf(hop.layer)]?.color}
                  strokeOpacity={0.6}
                />
                <text
                  x={x + 8}
                  y={y + 22}
                  fill="var(--text)"
                  fontSize={12.5}
                  fontFamily="var(--font-mono)"
                >
                  {clip(hop.name)}
                </text>
                <g data-story="cite">
                  <text
                    x={x + 8}
                    y={y + 42}
                    fill="var(--text-muted)"
                    fontSize={11}
                    fontFamily="var(--font-mono)"
                  >
                    {clip(`${hop.file}${hop.line === null ? "" : `:${String(hop.line)}`}`, 14)}
                  </text>
                  {hop.pip && (
                    <text
                      x={x + CARD_W - 8}
                      y={y + 22}
                      textAnchor="end"
                      fontSize={12}
                      fill={hop.pip === "verified" ? "var(--ok)" : "var(--warn)"}
                    >
                      {hop.pip === "verified" ? "●" : "◐"}
                    </text>
                  )}
                </g>
              </g>
            ))}
          </svg>
          <p className="text-xs text-fg-faint">
            The engine&apos;s own trace of {repo}: {hops.length} hops from the page to the data,
            each with its lines.
          </p>
        </div>
      </div>
    </section>
  );
}
