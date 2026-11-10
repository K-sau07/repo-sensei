/**
 * A diagram's exports (6.6): SVG drawn from its laid-out spec (cards, layer stripes, edges,
 * solid or dashed, with their labels; the architecture view's layer bands; the sequence view's
 * lifelines and numbered messages), PNG rendered from that SVG, and a file download.
 * Colors are fixed (a light blueprint), so an export reads the same wherever it is pasted.
 */
import { edgeWords, type Spec, type SpecEdge, type SpecNode } from "@/lib/api/diagrams";

import { NODE_HEIGHT, NODE_WIDTH, type Band, type Placed } from "./elk-layout";
import { CARD_HEIGHT, CARD_WIDTH, LOOP, sequenceOf } from "./sequence";

const PAD = 24;
const LANE_COLORS: Record<string, string> = {
  ui: "#7c5cff",
  state: "#7c5cff",
  client: "#1f8fff",
  route: "#1f8fff",
  handler: "#1f8fff",
  service: "#0aa37f",
  db: "#e0a000",
  other: "#8a8f98",
};

function escape(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

interface Sized {
  width: (node: SpecNode) => number;
  height: (node: SpecNode) => number;
}
const CARD: Sized = { width: () => NODE_WIDTH, height: () => NODE_HEIGHT };

export function exportSvg(
  spec: Spec,
  edges: readonly SpecEdge[],
  placed: Readonly<Record<string, Placed>>,
  { sized = CARD, bands = [] }: { sized?: Sized; bands?: readonly Band[] } = {},
): string {
  const at = (id: string) => placed[id] ?? { x: 0, y: 0 };
  const byId = new Map(spec.nodes.map((n) => [n.id, n]));
  const w = (id: string) => {
    const node = byId.get(id);
    return node ? sized.width(node) : NODE_WIDTH;
  };
  const h = (id: string) => {
    const node = byId.get(id);
    return node ? sized.height(node) : NODE_HEIGHT;
  };
  const xs = [...spec.nodes.map((n) => at(n.id).x), ...bands.map((b) => b.x)];
  const ys = [...spec.nodes.map((n) => at(n.id).y), ...bands.map((b) => b.y)];
  const rights = [
    ...spec.nodes.map((n) => at(n.id).x + w(n.id)),
    ...bands.map((b) => b.x + b.width),
  ];
  const bottoms = [
    ...spec.nodes.map((n) => at(n.id).y + h(n.id)),
    ...bands.map((b) => b.y + b.height),
  ];
  const minX = Math.min(0, ...xs) - PAD;
  const minY = Math.min(0, ...ys) - PAD;
  const width = Math.max(0, ...rights) + PAD - minX;
  const height = Math.max(0, ...bottoms) + PAD - minY;
  const parts: string[] = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${String(width)}" height="${String(height)}" viewBox="${String(minX)} ${String(minY)} ${String(width)} ${String(height)}" font-family="ui-sans-serif, system-ui, sans-serif">`,
    `<rect x="${String(minX)}" y="${String(minY)}" width="${String(width)}" height="${String(height)}" fill="#ffffff"/>`,
    `<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#6b7280"/></marker></defs>`,
  ];
  for (const band of bands) {
    const color = LANE_COLORS[band.tier.lane] ?? LANE_COLORS.other ?? "#8a8f98";
    parts.push(
      `<rect x="${String(band.x)}" y="${String(band.y)}" width="${String(band.width)}" height="${String(band.height)}" rx="12" fill="${color}" fill-opacity="0.06" stroke="${color}" stroke-opacity="0.45" stroke-dasharray="5 4"/>`,
      `<text x="${String(band.x + 16)}" y="${String(band.y + 24)}" font-size="12" font-weight="600" fill="${color}">${escape(band.tier.label.toUpperCase())}</text>`,
    );
  }
  for (const edge of edges) {
    const a = at(edge.from);
    const b = at(edge.to);
    const x1 = a.x + w(edge.from);
    const y1 = a.y + Math.min(h(edge.from), NODE_HEIGHT) / 2;
    const x2 = b.x;
    const y2 = b.y + Math.min(h(edge.to), NODE_HEIGHT) / 2;
    const dash = edge.verified ? "" : ' stroke-dasharray="6 4"';
    const color = edge.verified ? "#6b7280" : "#d97706";
    parts.push(
      `<line x1="${String(x1)}" y1="${String(y1)}" x2="${String(x2)}" y2="${String(y2)}" stroke="${color}" stroke-width="1.5"${dash} marker-end="url(#arrow)"/>`,
    );
    const label = edge.label ?? (edge.verified ? null : (edge.inferred?.reason ?? null));
    if (label) {
      parts.push(
        `<text x="${String((x1 + x2) / 2)}" y="${String((y1 + y2) / 2 - 4)}" font-size="11" fill="#4b5563" text-anchor="middle">${escape(label)}</text>`,
      );
    }
  }
  for (const node of spec.nodes) {
    const { x, y } = at(node.id);
    const color = LANE_COLORS[node.layer] ?? LANE_COLORS.other ?? "#8a8f98";
    const nodeHeight = h(node.id);
    parts.push(
      `<g><rect x="${String(x)}" y="${String(y)}" width="${String(w(node.id))}" height="${String(nodeHeight)}" rx="6" fill="#ffffff" stroke="#d1d5db"/>`,
      `<rect x="${String(x)}" y="${String(y)}" width="3" height="${String(nodeHeight)}" fill="${color}"/>`,
      `<text x="${String(x + 12)}" y="${String(y + 20)}" font-size="10" fill="${color}">${escape(node.layer.toUpperCase())}</text>`,
      `<text x="${String(x + 12)}" y="${String(y + 38)}" font-size="13" font-weight="600" fill="#111827">${escape(node.label)}</text>`,
      node.citation
        ? `<text x="${String(x + 12)}" y="${String(y + 54)}" font-size="10" font-family="ui-monospace, monospace" fill="#6b7280">${escape(node.citation.path)}:${String(node.citation.start_line)}</text>`
        : "",
      ...(node.members ?? []).map(
        (member, i) =>
          `<text x="${String(x + 12)}" y="${String(y + 78 + 20 * i)}" font-size="11" font-family="ui-monospace, monospace" fill="#4b5563">${escape(member.name)}</text>`,
      ),
      "</g>",
    );
  }
  parts.push("</svg>");
  return parts.join("");
}

/** The sequence view as SVG: its participants' cards, their lifelines, and the messages. */
export function exportSequenceSvg(spec: Spec, edges: readonly SpecEdge[]): string {
  const seq = sequenceOf(spec, edges);
  const width = seq.width + 2 * PAD;
  const height = seq.height + 2 * PAD;
  const parts: string[] = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${String(width)}" height="${String(height)}" viewBox="${String(-PAD)} ${String(-PAD)} ${String(width)} ${String(height)}" font-family="ui-sans-serif, system-ui, sans-serif">`,
    `<rect x="${String(-PAD)}" y="${String(-PAD)}" width="${String(width)}" height="${String(height)}" fill="#ffffff"/>`,
    `<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#6b7280"/></marker></defs>`,
  ];
  for (const p of seq.participants) {
    const color = LANE_COLORS[p.node.layer] ?? LANE_COLORS.other ?? "#8a8f98";
    const left = p.x - CARD_WIDTH / 2;
    parts.push(
      `<line x1="${String(p.x)}" y1="${String(CARD_HEIGHT)}" x2="${String(p.x)}" y2="${String(seq.height)}" stroke="#d1d5db" stroke-dasharray="4 4"/>`,
      `<rect x="${String(left)}" y="0" width="${String(CARD_WIDTH)}" height="${String(CARD_HEIGHT)}" rx="6" fill="#ffffff" stroke="#d1d5db"/>`,
      `<rect x="${String(left)}" y="0" width="3" height="${String(CARD_HEIGHT)}" fill="${color}"/>`,
      `<text x="${String(left + 12)}" y="22" font-size="10" fill="${color}">${escape(p.node.layer.toUpperCase())}</text>`,
      `<text x="${String(left + 12)}" y="42" font-size="13" font-weight="600" fill="#111827">${escape(p.node.label)}</text>`,
    );
  }
  for (const m of seq.messages) {
    const color = m.edge.verified ? "#6b7280" : "#d97706";
    const dash = m.edge.verified ? "" : ' stroke-dasharray="6 4"';
    parts.push(
      m.self
        ? `<path d="M${String(m.from)},${String(m.y - 8)} h${String(LOOP)} v16 h-${String(LOOP)}" fill="none" stroke="${color}" stroke-width="1.5"${dash} marker-end="url(#arrow)"/>`
        : `<line x1="${String(m.from)}" y1="${String(m.y)}" x2="${String(m.to)}" y2="${String(m.y)}" stroke="${color}" stroke-width="1.5"${dash} marker-end="url(#arrow)"/>`,
      `<text x="${String(m.self ? m.from + LOOP + 8 : (m.from + m.to) / 2)}" y="${String(m.y - 7)}" font-size="11" fill="#4b5563" text-anchor="middle">${String(m.n)}. ${escape(edgeWords(m.edge))}</text>`,
    );
  }
  parts.push("</svg>");
  return parts.join("");
}

/** The SVG rendered to a PNG (at twice the size, for sharp text). */
export async function exportPng(
  spec: Spec,
  edges: readonly SpecEdge[],
  placed: Readonly<Record<string, Placed>>,
): Promise<Blob> {
  return svgToPng(exportSvg(spec, edges, placed));
}

/** An SVG drawn to a PNG (at twice the size, for sharp text). */
export async function svgToPng(svg: string): Promise<Blob> {
  const image = new Image();
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
  try {
    await new Promise<void>((resolve, reject) => {
      image.onload = () => {
        resolve();
      };
      image.onerror = () => {
        reject(new Error("the SVG couldn't be drawn"));
      };
      image.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = image.width * 2;
    canvas.height = image.height * 2;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("no 2D canvas");
    context.scale(2, 2);
    context.drawImage(image, 0, 0);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (blob) resolve(blob);
        else reject(new Error("the PNG couldn't be made"));
      }, "image/png");
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** Save ``data`` as a file named ``name``. */
export function download(name: string, data: Blob | string, type = "application/octet-stream") {
  const blob = typeof data === "string" ? new Blob([data], { type }) : data;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name.replace(/[\\/:*?"<>|]+/g, "-");
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 1000);
}
