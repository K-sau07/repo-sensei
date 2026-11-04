"use client";

/**
 * The hero's sky (10.3): the picture drawn on the server first (so the headline, not the WebGL,
 * is what paints first), then, once the page is idle, on a GPU, and with motion allowed, the
 * WebGL scene fades in over it. It draws only on screen in a visible page, and can be paused
 * (it moves for longer than five seconds). Reduced motion, a slow or software-rendered device,
 * or no WebGL2 keep the picture.
 */
import { cn } from "cn";
import { Pause, Play } from "lucide-react";
import { useReducedMotionConfig } from "motion/react";
import dynamic from "next/dynamic";
import { useCallback, useRef, useState, useSyncExternalStore } from "react";

import { useActive, useIdle } from "@/hooks/use-idle";
import { isLowPower } from "@/lib/constellation";
import type { HeroSky as Sky } from "@/lib/demo/hero-sky";
import { webGLSupport } from "@/lib/webgl";

const HeroSkyCanvas = dynamic(() => import("./hero-sky-canvas"), { ssr: false });

const noSubscribe = () => () => undefined;

export function HeroSky({ sky, picture }: { sky: Sky; picture: React.ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const client = useSyncExternalStore(
    noSubscribe,
    () => true,
    () => false,
  );
  const reduced = useReducedMotionConfig() ?? false;
  const idle = useIdle();
  const onScreen = useActive(box);
  const [paused, setPaused] = useState(false);
  const [ready, setReady] = useState(false);
  const onReady = useCallback(() => {
    setReady(true);
  }, []);
  const probed = client && idle && !reduced ? webGLSupport() : null;
  const webgl = probed !== null && probed.webgl2 && !probed.software && !isLowPower(navigator);

  return (
    <div ref={box} className="absolute inset-0" data-testid="hero-sky" data-webgl={webgl}>
      <div
        data-layer
        className={cn(
          "absolute inset-0 transition-opacity duration-1000",
          webgl && ready ? "opacity-0" : "opacity-100",
        )}
      >
        {picture}
      </div>
      {webgl && (
        <div
          data-layer
          className={cn(
            "absolute inset-0 transition-opacity duration-1000",
            ready ? "opacity-100" : "opacity-0",
          )}
        >
          <HeroSkyCanvas sky={sky} active={onScreen && !paused} onReady={onReady} />
        </div>
      )}
      {webgl && ready && (
        <button
          type="button"
          aria-pressed={paused}
          onClick={() => {
            setPaused((was) => !was);
          }}
          className="absolute right-4 bottom-4 z-10 inline-flex items-center gap-1.5 rounded-full border border-line bg-bg/70 px-3 py-1.5 text-xs text-fg-muted backdrop-blur hover:text-fg"
        >
          {paused ? (
            <Play aria-hidden className="size-3" />
          ) : (
            <Pause aria-hidden className="size-3" />
          )}
          {paused ? "Play motion" : "Pause motion"}
        </button>
      )}
    </div>
  );
}
