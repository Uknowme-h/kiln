/* Kiln pixel engine.
 *
 * Everything on the floor is drawn procedurally: characters, desks, plants,
 * the CI rack. There are no image assets, so there is nothing to license and
 * nothing that predates the hackathon. Sprites are drawn as 1-bit-per-pixel
 * buffers on tiny offscreen canvases, then blitted up with smoothing off,
 * which is what gives the chunky 16-bit look.
 *
 * The character system is our own: a recipe (skin, hair, build, outfit,
 * accessory) is compiled once into a small set of animation frames.
 */
"use strict";

const Kiln = (window.Kiln = window.Kiln || {});

// ─── design tokens ────────────────────────────────────────────────────────────
Kiln.T = {
  cream: { 50: "#FFFDF5", 100: "#FFF8E7", 200: "#F4E9C7", 300: "#E8D9A0" },
  paper: { 100: "#FCFAF0", 200: "#F0EAD2" },
  ink: { 900: "#1A1320", 700: "#3D2E4A", 500: "#6B5878", 300: "#A899B5", 100: "#D9CFE0" },
  accent: {
    coral: "#E06A62", mint: "#5FB07F", sky: "#4FA3AF", lemon: "#D8A93C",
    lilac: "#9A82D3", peach: "#DE9668",
  },
  status: {
    idle: "#A199AB", thinking: "#4FA3AF", working: "#D8A93C",
    stuck: "#E06A62", done: "#5FB07F",
  },
  world: {
    floorA: "#E9D9B8", floorB: "#DCC9A0", rug: "#C98A6B", rugEdge: "#B4704F",
    wood: "#C9A66B", woodDark: "#A88450", wall: "#8B6F47", rack: "#3A2E4A",
  },
  tile: 32,
};

// ─── tiny pixel-buffer helpers ────────────────────────────────────────────────
// A sprite is drawn into a Uint8ClampedArray, then put on a 1× canvas once.
function makeBuf(w, h) {
  return { w, h, data: new Uint8ClampedArray(w * h * 4) };
}
function px(buf, x, y, c) {
  if (x < 0 || x >= buf.w || y < 0 || y >= buf.h) return;
  const i = (y * buf.w + x) * 4;
  buf.data[i] = c[0]; buf.data[i + 1] = c[1]; buf.data[i + 2] = c[2]; buf.data[i + 3] = 255;
}
function rect(buf, x0, y0, x1, y1, c) {
  for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) px(buf, x, y, c);
}
function alpha(buf, x, y) {
  if (x < 0 || x >= buf.w || y < 0 || y >= buf.h) return 0;
  return buf.data[(y * buf.w + x) * 4 + 3];
}
function hex(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}
function shade(c, f) {
  return [Math.min(255, c[0] * f), Math.min(255, c[1] * f), Math.min(255, c[2] * f)].map(Math.round);
}
// Auto-outline: any transparent pixel touching an opaque one becomes ink.
function outline(buf, c) {
  const pts = [];
  for (let y = 0; y < buf.h; y++)
    for (let x = 0; x < buf.w; x++) {
      if (alpha(buf, x, y) !== 0) continue;
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]])
        if (alpha(buf, x + dx, y + dy) === 255) { pts.push([x, y]); break; }
    }
  for (const [x, y] of pts) px(buf, x, y, c);
}
function toCanvas(buf) {
  const c = document.createElement("canvas");
  c.width = buf.w; c.height = buf.h;
  const ctx = c.getContext("2d");
  const img = ctx.createImageData(buf.w, buf.h);
  img.data.set(buf.data);
  ctx.putImageData(img, 0, 0);
  return c;
}

Kiln.px = { makeBuf, px, rect, alpha, hex, shade, outline, toCanvas };
