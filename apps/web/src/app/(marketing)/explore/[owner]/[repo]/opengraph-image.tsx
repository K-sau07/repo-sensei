import { notFound } from "next/navigation";
import { ImageResponse } from "next/og";

import { shortSha } from "@/lib/api/explore";
import { publicRepo } from "@/lib/api/explore-server";

export const alt = "A repository indexed by repo-sensei";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const SUMMARY_CHARS = 220;

/** The page's social card: its name, what it does, the commit it was verified at. */
export default async function Image({
  params,
}: {
  params: Promise<{ owner: string; repo: string }>;
}) {
  const { owner, repo } = await params;
  const page = await publicRepo(owner, repo);
  if (page === null) notFound(); // never a card in the site's name for what isn't published
  const name = page.repo.full_name;
  const summary = page.repo.summary ?? "";
  return new ImageResponse(
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        padding: 72,
        background: "#0c0d0f",
        color: "#f4f4f5",
      }}
    >
      <div style={{ display: "flex", fontSize: 28, color: "#a1a1aa" }}>repo-sensei · explore</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
        <div style={{ display: "flex", fontSize: 64, fontWeight: 700 }}>{name}</div>
        {summary && (
          <div style={{ display: "flex", fontSize: 30, color: "#d4d4d8", lineHeight: 1.35 }}>
            {summary.length > SUMMARY_CHARS ? `${summary.slice(0, SUMMARY_CHARS - 1)}…` : summary}
          </div>
        )}
      </div>
      <div style={{ display: "flex", fontSize: 26, color: "#a1a1aa" }}>
        {`Verified at ${shortSha(page.repo.sha)}`}
      </div>
    </div>,
    size,
  );
}
