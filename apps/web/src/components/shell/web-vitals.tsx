"use client";

/**
 * Web vitals (10.11): the page's LCP, INP and CLS, as the browser measures them, sent to the
 * org's insights with the route as a pattern (no names). Held until the page is hidden, then
 * sent at once (a request that outlives the page); failing to send is nobody's concern.
 */
import { useParams, usePathname } from "next/navigation";
import { useReportWebVitals } from "next/web-vitals";
import { useEffect, useRef } from "react";

import { api } from "@/lib/api/client";
import { routePattern } from "@/lib/vitals/route";

interface Vital {
  name: "LCP" | "INP" | "CLS";
  value: number;
  route: string;
}

/** What it reports of a measure (Next's compiled web-vitals ships no types of its own). */
interface Measure {
  name: string;
  value: number;
}

const MEASURED = new Set(["LCP", "INP", "CLS"]);
const BATCH = 10; // what the API takes in one report

export function WebVitals() {
  const params = useParams<Record<string, string | string[]>>();
  const pathname = usePathname();
  const org = typeof params.org === "string" ? params.org : "";
  const held = useRef<Vital[]>([]);
  const where = useRef({ org: "", route: "/" });
  useEffect(() => {
    where.current = { org, route: routePattern(pathname, params) };
  }, [org, pathname, params]);

  useReportWebVitals((metric: Measure) => {
    if (!MEASURED.has(metric.name)) return;
    held.current.push({
      name: metric.name as Vital["name"],
      value: metric.value,
      route: where.current.route,
    });
    if (held.current.length >= BATCH) send(where.current.org, held);
  });

  useEffect(() => {
    const flush = () => {
      if (document.visibilityState === "hidden") send(where.current.org, held);
    };
    document.addEventListener("visibilitychange", flush);
    window.addEventListener("pagehide", flush);
    return () => {
      document.removeEventListener("visibilitychange", flush);
      window.removeEventListener("pagehide", flush);
    };
  }, []);

  return null;
}

function send(org: string, held: { current: Vital[] }) {
  while (held.current.length > 0) {
    const vitals = held.current.splice(0, BATCH);
    if (org === "") continue; // not in an org's pages yet: nowhere to count them
    void api
      .POST("/api/v1/orgs/{org}/vitals", {
        params: { path: { org } },
        body: { vitals },
        keepalive: true,
      })
      .catch(() => undefined);
  }
}
