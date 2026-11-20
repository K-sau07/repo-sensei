import { ImageResponse } from "next/og";

export const alt = "repo-sensei: understand any codebase, with answers you can verify";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

/** The site's social card, wherever a page has none of its own: what it is, who builds it. */
export default function Image() {
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
      <div style={{ display: "flex", fontSize: 28, color: "#a1a1aa" }}>reposensei.com</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
        <div style={{ display: "flex", fontSize: 88, fontWeight: 700 }}>repo-sensei</div>
        <div style={{ display: "flex", fontSize: 40, color: "#d4d4d8", lineHeight: 1.3 }}>
          Understand any codebase, with answers that cite the lines they stand on.
        </div>
      </div>
      <div style={{ display: "flex", fontSize: 26, color: "#a1a1aa" }}>
        Built by Saurabh Kashyap
      </div>
    </div>,
    size,
  );
}
