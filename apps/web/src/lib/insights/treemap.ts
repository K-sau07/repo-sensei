/**
 * A squarified treemap (Bruls, Huizing and van Wijk): each item a rectangle whose area is its
 * share of the total, laid out in rows that keep the rectangles as close to square as they can
 * be, largest first. Items of no value get no rectangle.
 */

export interface Tile<T> {
  item: T;
  x: number;
  y: number;
  width: number;
  height: number;
}

interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** The worst aspect ratio in a row of `areas` laid along a side of length `side`. */
function worst(areas: number[], side: number): number {
  const sum = areas.reduce((a, b) => a + b, 0);
  const max = Math.max(...areas);
  const min = Math.min(...areas);
  const s2 = side * side;
  const sum2 = sum * sum;
  return Math.max((s2 * max) / sum2, sum2 / (s2 * min));
}

export function treemap<T>(
  items: readonly T[],
  value: (item: T) => number,
  width: number,
  height: number,
): Tile<T>[] {
  const sorted = items
    .map((item) => ({ item, value: value(item) }))
    .filter((entry) => entry.value > 0)
    .sort((a, b) => b.value - a.value);
  const total = sorted.reduce((sum, entry) => sum + entry.value, 0);
  if (total === 0 || width <= 0 || height <= 0) return [];
  const scale = (width * height) / total;
  const queue = sorted.map((entry) => ({ item: entry.item, area: entry.value * scale }));

  const tiles: Tile<T>[] = [];
  let free: Rect = { x: 0, y: 0, width, height };
  let row: typeof queue = [];

  const place = (laid: typeof queue) => {
    const sum = laid.reduce((s, e) => s + e.area, 0);
    if (free.width >= free.height) {
      // A column on the left, as wide as the row's area needs.
      const columnWidth = sum / free.height;
      let y = free.y;
      for (const entry of laid) {
        const h = entry.area / columnWidth;
        tiles.push({ item: entry.item, x: free.x, y, width: columnWidth, height: h });
        y += h;
      }
      free = { ...free, x: free.x + columnWidth, width: free.width - columnWidth };
    } else {
      const rowHeight = sum / free.width;
      let x = free.x;
      for (const entry of laid) {
        const w = entry.area / rowHeight;
        tiles.push({ item: entry.item, x, y: free.y, width: w, height: rowHeight });
        x += w;
      }
      free = { ...free, y: free.y + rowHeight, height: free.height - rowHeight };
    }
  };

  for (const entry of queue) {
    const side = Math.min(free.width, free.height);
    const areas = row.map((e) => e.area);
    if (row.length === 0 || worst([...areas, entry.area], side) <= worst(areas, side)) {
      row.push(entry);
    } else {
      place(row);
      row = [entry];
    }
  }
  if (row.length > 0) place(row);
  return tiles;
}
