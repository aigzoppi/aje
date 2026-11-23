// Fractal helpers: views, tiles, payloads and colouring. Mirrors aje_worker/examples/fractal.py.

export const WORKER_COMMAND = ["python", "-m", "aje_worker", "aje_worker.examples.fractal:tile"];

export const JULIA_PRESETS = [
  { name: "Dendrite", c: [0, 1] },
  { name: "Douady rabbit", c: [-0.123, 0.745] },
  { name: "San Marco", c: [-0.75, 0.0] },
  { name: "Siegel disk", c: [-0.391, -0.587] },
  { name: "Spiral", c: [-0.8, 0.156] },
  { name: "Galaxies", c: [0.285, 0.01] },
];

export function defaultView(kind, aspect = 1) {
  const [cx, cy, h] = kind === "mandelbrot" ? [-0.6, 0, 2.6] : [0, 0, 3.0];
  const w = h * aspect;
  return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2];
}

// Zoom around the clicked pixel: factor < 1 zooms in.
export function zoomView(view, image, px, py, factor) {
  const [x0, y0, x1, y1] = view;
  const [w, h] = image;
  const cx = x0 + ((px + 0.5) / w) * (x1 - x0);
  const cy = y1 - ((py + 0.5) / h) * (y1 - y0);
  const hw = ((x1 - x0) * factor) / 2;
  const hh = ((y1 - y0) * factor) / 2;
  return [cx - hw, cy - hh, cx + hw, cy + hh];
}

export function splitImage(width, height, tileSize) {
  const tiles = [];
  for (let y = 0; y < height; y += tileSize)
    for (let x = 0; x < width; x += tileSize)
      tiles.push([x, y, Math.min(tileSize, width - x), Math.min(tileSize, height - y)]);
  return tiles;
}

export function tilePayloads({ kind, c, view, size, tileSize, maxIter }) {
  return splitImage(size, size, tileSize).map((tile) => ({
    kind, c, view, image: [size, size], tile, max_iter: maxIter,
  }));
}

export function tileKey(tile) {
  return tile.join(",");
}

export function decodeTile(result) {
  const bin = atob(result.data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Uint16Array(bytes.buffer); // little-endian uint16, like the worker packs it
}

// Smooth cyclic palette (cosine gradient), black inside the set.
const PALETTE = (() => {
  const n = 1024;
  const out = new Uint8ClampedArray(n * 3);
  for (let i = 0; i < n; i++) {
    const t = i / n;
    out[i * 3] = 255 * (0.5 + 0.5 * Math.cos(6.2832 * (t + 0.0)));
    out[i * 3 + 1] = 255 * (0.5 + 0.5 * Math.cos(6.2832 * (t + 0.15)));
    out[i * 3 + 2] = 255 * (0.5 + 0.5 * Math.cos(6.2832 * (t + 0.35)));
  }
  return out;
})();

export function tileToImageData(ctx, result) {
  const [, , w, h] = result.tile;
  const values = decodeTile(result);
  const img = ctx.createImageData(w, h);
  const max = result.max_iter;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    const o = i * 4;
    if (v >= max) {
      img.data[o] = 6; img.data[o + 1] = 6; img.data[o + 2] = 14;
    } else {
      const p = (Math.floor(Math.sqrt(v / max) * 1024 * 3) % 1024) * 3;
      img.data[o] = PALETTE[p]; img.data[o + 1] = PALETTE[p + 1]; img.data[o + 2] = PALETTE[p + 2];
    }
    img.data[o + 3] = 255;
  }
  return img;
}

// Stable distinct colour per worker id.
const WORKER_COLORS = ["#22d3ee", "#f472b6", "#a3e635", "#fbbf24", "#a78bfa", "#fb7185", "#34d399", "#60a5fa",
  "#f97316", "#e879f9", "#2dd4bf", "#facc15", "#c084fc", "#4ade80", "#38bdf8", "#fda4af"];

export function workerColor(workers, id) {
  const i = workers.indexOf(id);
  return WORKER_COLORS[(i < 0 ? 0 : i) % WORKER_COLORS.length];
}
