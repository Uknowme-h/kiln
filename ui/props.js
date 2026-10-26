/* Procedural set pieces for the Kiln floor: desk, plant, CI rack, task board,
 * and the speech bubbles. All drawn once into small canvases and reused.
 */
"use strict";

const KilnProps = (() => {
  const { makeBuf, px, rect, hex, shade, outline, toCanvas } = Kiln.px;
  const T = Kiln.T;
  const INK = hex(T.ink[900]);
  const W = T.world;

  // A wooden desk with a little monitor that can glow. 48×28.
  function desk(screen) {
    const buf = makeBuf(48, 28);
    rect(buf, 2, 12, 45, 22, hex(W.wood)); // desktop
    rect(buf, 2, 12, 45, 14, hex(W.woodDark)); // front edge
    rect(buf, 4, 22, 7, 26, hex(W.woodDark)); // legs
    rect(buf, 40, 22, 43, 26, hex(W.woodDark));
    // monitor
    rect(buf, 18, 2, 30, 12, hex(T.ink[700]));
    rect(buf, 20, 4, 28, 10, screen || hex(T.ink[500]));
    rect(buf, 22, 12, 26, 13, hex(T.ink[500])); // stand
    // mug
    rect(buf, 36, 8, 39, 12, hex(T.accent.coral)); px(buf, 40, 9, hex(T.accent.coral));
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A potted plant for corners. 20×26.
  function plant() {
    const buf = makeBuf(20, 26);
    const leaf = hex(T.accent.mint), leafD = shade(leaf, 0.7);
    rect(buf, 6, 16, 13, 24, hex(W.rugEdge)); // pot
    rect(buf, 5, 16, 14, 18, shade(hex(W.rugEdge), 1.15)); // rim
    rect(buf, 8, 8, 11, 16, leafD); // stem cluster
    for (const [x, y] of [[6,6],[9,3],[12,5],[7,9],[11,9],[9,7],[5,11],[13,11],[9,12]]) px(buf, x, y, leaf);
    for (const [x, y] of [[7,5],[10,4],[8,8],[11,7]]) px(buf, x, y, shade(leaf, 1.2));
    outline(buf, INK);
    return toCanvas(buf);
  }

  // The CI rack: a server tower whose light shows the last test run. 40×56.
  function rack(state) {
    const buf = makeBuf(40, 56);
    rect(buf, 4, 2, 35, 53, hex(W.rack));
    for (let i = 0; i < 4; i++) rect(buf, 8, 8 + i * 12, 31, 15 + i * 12, hex(T.ink[700]));
    const light = state === "green" ? hex(T.accent.mint) : state === "red" ? hex(T.accent.coral) : hex(T.ink[500]);
    for (let i = 0; i < 4; i++) { px(buf, 28, 10 + i * 12, light); px(buf, 29, 10 + i * 12, light); }
    outline(buf, INK);
    return toCanvas(buf);
  }

  // The task board: a cork board. Sticky notes are pinned on it by the scene,
  // one per assigned task, so the board fills as the planner hands out work.
  function board() {
    const buf = makeBuf(56, 36);
    rect(buf, 2, 2, 53, 33, hex(W.wood)); // frame
    rect(buf, 4, 4, 51, 31, hex("#C99A5B")); // cork
    rect(buf, 8, 8, 15, 16, hex(T.cream[50])); // a pinned paper
    px(buf, 10, 11, INK); px(buf, 10, 13, INK);
    rect(buf, 42, 6, 49, 14, hex(T.cream[100])); // another paper
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A chunky speech bubble with a tail. Drawn per-status with its symbol.
  function bubble(kind) {
    const buf = makeBuf(20, 18);
    const fill = hex(T.cream[50]);
    rect(buf, 2, 1, 17, 12, fill);
    px(buf, 9, 13, fill); px(buf, 10, 13, fill); px(buf, 9, 14, fill); // tail
    const c = kind === "stuck" ? hex(T.accent.coral) : kind === "done" ? hex(T.accent.mint)
      : kind === "working" ? hex(T.accent.lemon) : hex(T.ink[500]);
    if (kind === "working") { // three dots
      px(buf, 6, 7, c); px(buf, 10, 7, c); px(buf, 14, 7, c);
    } else if (kind === "stuck") { // !
      rect(buf, 9, 4, 10, 8, c); px(buf, 9, 10, c); px(buf, 10, 10, c);
    } else if (kind === "done") { // check
      px(buf, 6, 8, c); px(buf, 7, 9, c); px(buf, 8, 10, c); px(buf, 9, 9, c);
      px(buf, 10, 8, c); px(buf, 11, 7, c); px(buf, 12, 6, c); px(buf, 13, 5, c);
    } else { // thinking: ellipsis ring
      px(buf, 7, 7, c); px(buf, 10, 6, c); px(buf, 13, 7, c);
    }
    outline(buf, INK);
    return toCanvas(buf);
  }

  function disc(buf, cx, cy, r, c) {
    for (let y = -r; y <= r; y++)
      for (let x = -r; x <= r; x++)
        if (x * x + y * y <= r * r) px(buf, cx + x, cy + y, c);
  }

  // Meeting corner: a big table with chairs, papers and a laptop. 80×44.
  function conferenceTable() {
    const buf = makeBuf(80, 44);
    const wood = hex(W.wood), woodD = hex(W.woodDark);
    const cush = hex(T.accent.sky), cushD = shade(cush, 0.75);
    for (const cx of [22, 40, 58]) { // chairs, three a side
      rect(buf, cx - 5, 1, cx + 5, 4, cushD); rect(buf, cx - 5, 5, cx + 5, 9, cush); // top row
      rect(buf, cx - 5, 34, cx + 5, 38, cush); rect(buf, cx - 5, 39, cx + 5, 42, cushD); // bottom row
    }
    rect(buf, 10, 13, 69, 32, wood); // tabletop
    rect(buf, 10, 13, 69, 14, shade(wood, 1.15)); // top light
    rect(buf, 10, 30, 69, 32, woodD); // front edge
    rect(buf, 36, 17, 43, 23, hex(T.ink[700])); rect(buf, 37, 18, 42, 21, hex(T.ink[500])); // laptop
    rect(buf, 16, 18, 20, 22, hex(T.cream[50])); rect(buf, 57, 19, 61, 23, hex(T.cream[50])); // papers
    rect(buf, 26, 24, 28, 27, hex(T.accent.coral)); rect(buf, 51, 15, 53, 18, hex(T.accent.lemon)); // mugs
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A whiteboard on an easel, with a little flowchart scribbled on it. 64×44.
  function whiteboard() {
    const buf = makeBuf(64, 44);
    const wood = hex(W.woodDark);
    for (let i = 0; i < 11; i++) { // easel legs
      px(buf, 20 - Math.floor(i / 3), 32 + i, wood);
      px(buf, 43 + Math.floor(i / 3), 32 + i, wood);
    }
    rect(buf, 8, 31, 55, 33, wood); // marker tray
    px(buf, 14, 32, hex(T.accent.coral)); px(buf, 18, 32, hex(T.accent.sky)); // markers
    rect(buf, 6, 2, 57, 30, hex(T.cream[50])); // board
    // scribbles: two boxes and an arrow, then a wavy line
    rect(buf, 10, 6, 20, 12, hex(T.accent.sky)); rect(buf, 11, 7, 19, 11, hex(T.cream[50]));
    rect(buf, 40, 6, 50, 12, hex(T.accent.mint)); rect(buf, 41, 7, 49, 11, hex(T.cream[50]));
    for (let x = 23; x <= 37; x++) px(buf, x, 9, INK);
    px(buf, 36, 8, INK); px(buf, 36, 10, INK);
    for (let x = 10; x <= 50; x++) px(buf, x, 20 + Math.round(2 * Math.sin(x / 3)), hex(T.accent.coral));
    px(buf, 12, 26, hex(T.accent.lemon)); px(buf, 46, 26, hex(T.accent.lemon));
    outline(buf, INK);
    return toCanvas(buf);
  }

  // Docs corner: a bookshelf with colourful spines. 48×64.
  function bookshelf() {
    const buf = makeBuf(48, 64);
    const wood = hex(W.wood), woodD = hex(W.woodDark);
    rect(buf, 2, 2, 45, 63, wood);
    rect(buf, 5, 6, 42, 60, woodD);
    rect(buf, 5, 24, 42, 25, wood); rect(buf, 5, 42, 42, 43, wood); // shelves
    const C = [T.accent.coral, T.accent.sky, T.accent.lemon, T.accent.lilac, T.accent.mint, T.accent.peach].map(hex);
    const rows = [
      [[6, 3, 12], [10, 2, 10], [13, 3, 13], [17, 2, 9], [20, 3, 11], [24, 2, 12], [28, 3, 10], [33, 2, 11], [37, 3, 9]],
      [[6, 2, 11], [10, 3, 9], [14, 2, 13], [18, 3, 10], [23, 2, 12], [27, 3, 11], [31, 2, 9], [35, 3, 12]],
      [[7, 3, 10], [11, 2, 12], [15, 3, 9], [19, 2, 11], [24, 3, 13], [29, 2, 10], [34, 3, 11], [38, 2, 9]],
    ];
    rows.forEach((books, s) => {
      const base = [23, 41, 59][s];
      books.forEach(([x, w, h], i) => rect(buf, x, base - h, x + w - 1, base - 1, C[(i + s * 2) % C.length]));
    });
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A three-drawer file cabinet. 36×48.
  function fileCabinet() {
    const buf = makeBuf(36, 48);
    rect(buf, 4, 2, 31, 46, hex(T.cream[300]));
    rect(buf, 29, 4, 31, 46, shade(hex(T.cream[300]), 0.8)); // side shade
    for (const y of [6, 18, 30]) {
      rect(buf, 6, y, 28, y + 9, shade(hex(T.cream[300]), 0.92));
      rect(buf, 15, y + 4, 19, y + 5, hex(T.ink[500])); // handle
      rect(buf, 8, y + 3, 12, y + 5, hex(T.cream[50])); // label
    }
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A printer on a small table, paper loaded. 44×30.
  function printerTable() {
    const buf = makeBuf(44, 30);
    rect(buf, 4, 18, 6, 29, hex(W.woodDark)); rect(buf, 37, 18, 39, 29, hex(W.woodDark)); // legs
    rect(buf, 2, 15, 41, 18, hex(W.wood)); // tabletop
    rect(buf, 16, 0, 27, 5, hex(T.cream[50])); // paper
    rect(buf, 10, 4, 33, 15, hex(T.paper[100])); // printer body
    rect(buf, 10, 4, 33, 6, shade(hex(T.paper[100]), 0.85));
    rect(buf, 13, 12, 30, 13, hex(T.ink[700])); // output slot
    px(buf, 30, 8, hex(T.accent.coral)); px(buf, 27, 8, hex(T.accent.mint)); // buttons
    outline(buf, INK);
    return toCanvas(buf);
  }

  // Break nook: a two-seat couch with throw pillows. 72×36.
  function couch() {
    const buf = makeBuf(72, 36);
    const base = hex(T.accent.lilac), dark = shade(base, 0.75), light = shade(base, 1.15);
    rect(buf, 4, 2, 67, 12, base); // back
    for (let y = 3; y <= 11; y++) px(buf, 35, y, dark); // cushion split
    rect(buf, 0, 8, 8, 32, base); rect(buf, 63, 8, 71, 32, base); // arms
    rect(buf, 0, 8, 8, 10, light); rect(buf, 63, 8, 71, 10, light);
    rect(buf, 9, 14, 34, 28, base); rect(buf, 37, 14, 62, 28, base); // seat cushions
    rect(buf, 9, 26, 62, 28, dark);
    rect(buf, 12, 10, 20, 17, hex(T.accent.lemon)); rect(buf, 52, 10, 60, 17, hex(T.accent.mint)); // pillows
    rect(buf, 6, 32, 10, 35, hex(T.ink[700])); rect(buf, 61, 32, 65, 35, hex(T.ink[700])); // legs
    outline(buf, INK);
    return toCanvas(buf);
  }

  // The classic water cooler. 20×40.
  function waterCooler() {
    const buf = makeBuf(20, 40);
    rect(buf, 6, 2, 13, 12, hex("#A8D8E0")); // bottle
    rect(buf, 7, 3, 8, 10, hex("#CDEBF0")); // bottle highlight
    rect(buf, 8, 12, 11, 14, hex(T.ink[300])); // neck
    rect(buf, 4, 14, 15, 38, hex(T.cream[50])); // body
    px(buf, 7, 20, hex(T.accent.coral)); px(buf, 12, 20, hex(T.accent.sky)); // taps
    rect(buf, 5, 24, 14, 25, hex(T.ink[300])); // drip tray
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A counter with a coffee machine and mugs. 48×26.
  function coffeeCounter() {
    const buf = makeBuf(48, 26);
    rect(buf, 0, 10, 47, 25, hex(W.wood)); rect(buf, 0, 8, 47, 11, hex(T.cream[200])); // counter
    rect(buf, 5, 0, 17, 9, hex(T.ink[700])); // machine
    rect(buf, 7, 5, 12, 9, hex("#A8D8E0")); // pot
    px(buf, 6, 1, hex(T.accent.coral)); // power light
    rect(buf, 24, 6, 27, 9, hex(T.accent.coral)); rect(buf, 32, 6, 35, 9, hex(T.accent.lemon)); // mugs
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A tall ficus for corners. 24×40.
  function bigPlant() {
    const buf = makeBuf(24, 40);
    const leaf = hex(T.accent.mint), dark = shade(leaf, 0.7), light = shade(leaf, 1.2);
    rect(buf, 7, 30, 16, 39, hex(W.rugEdge)); rect(buf, 6, 30, 17, 32, shade(hex(W.rugEdge), 1.15)); // pot
    rect(buf, 11, 20, 12, 30, hex(W.wall)); // trunk
    disc(buf, 12, 10, 8, leaf); disc(buf, 6, 16, 5, leaf); disc(buf, 18, 16, 5, leaf);
    disc(buf, 9, 7, 3, light); disc(buf, 14, 19, 3, dark);
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A mesh trash bin with a crumpled paper ball. 10×14.
  function trashBin() {
    const buf = makeBuf(10, 14);
    rect(buf, 1, 1, 8, 3, hex(T.ink[500])); // rim
    rect(buf, 2, 3, 7, 12, hex(T.ink[300])); // body
    for (const x of [3, 5]) for (let y = 4; y <= 11; y++) px(buf, x, y, shade(hex(T.ink[300]), 0.75));
    px(buf, 4, 0, hex(T.cream[50])); px(buf, 5, 1, hex(T.cream[50])); // paper ball
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A wall window with a little sky. 32×18.
  function wallWindow() {
    const buf = makeBuf(32, 18);
    rect(buf, 0, 0, 31, 17, hex(T.cream[50])); // frame
    rect(buf, 2, 2, 29, 15, hex("#A8D8E0")); // sky
    rect(buf, 2, 10, 29, 15, hex("#CDEBF0")); // horizon light
    rect(buf, 15, 2, 16, 15, hex(T.cream[50])); rect(buf, 2, 8, 29, 9, hex(T.cream[50])); // mullions
    rect(buf, 6, 4, 11, 6, hex(T.cream[50])); px(buf, 8, 3, hex(T.cream[50])); // cloud
    rect(buf, 20, 11, 25, 13, hex(T.cream[50])); // cloud
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A framed poster: a rocket going up. 28×18.
  function posterFrame() {
    const buf = makeBuf(28, 18);
    rect(buf, 0, 0, 27, 17, hex(W.woodDark)); // frame
    rect(buf, 2, 2, 25, 15, hex(T.ink[700])); // space
    px(buf, 5, 4, hex(T.cream[50])); px(buf, 22, 5, hex(T.cream[50])); px(buf, 8, 13, hex(T.cream[50])); // stars
    rect(buf, 13, 4, 15, 9, hex(T.cream[100])); // body
    px(buf, 14, 2, hex(T.accent.coral)); px(buf, 14, 3, hex(T.accent.coral)); // nose
    px(buf, 12, 9, hex(T.accent.coral)); px(buf, 16, 9, hex(T.accent.coral));
    px(buf, 12, 10, hex(T.accent.coral)); px(buf, 16, 10, hex(T.accent.coral)); // fins
    px(buf, 14, 6, hex(T.accent.sky)); // window
    px(buf, 14, 11, hex(T.accent.lemon)); px(buf, 14, 12, hex(T.accent.lemon)); px(buf, 13, 11, hex(T.accent.coral)); // flame
    outline(buf, INK);
    return toCanvas(buf);
  }

  // A wooden plank sign; text is drawn over it by the scene. w×h.
  function plank(w, h) {
    const buf = makeBuf(w, h);
    rect(buf, 0, 0, w - 1, h - 1, hex(W.wood));
    rect(buf, 0, 0, w - 1, 1, shade(hex(W.wood), 1.2));
    rect(buf, 0, h - 2, w - 1, h - 1, hex(W.woodDark));
    px(buf, 3, Math.floor(h / 2), INK); px(buf, w - 4, Math.floor(h / 2), INK);
    outline(buf, INK);
    return toCanvas(buf);
  }

  // Clock face as a raw buffer; the scene stamps hands on a copy each minute. 24×24.
  function clockFace() {
    const buf = makeBuf(24, 24);
    disc(buf, 12, 12, 11, INK);
    disc(buf, 12, 12, 9, hex(T.cream[50]));
    px(buf, 12, 4, INK); px(buf, 12, 20, INK); px(buf, 4, 12, INK); px(buf, 20, 12, INK);
    return buf;
  }

  return {
    desk, plant, rack, board, bubble,
    conferenceTable, whiteboard, bookshelf, fileCabinet, printerTable,
    couch, waterCooler, coffeeCounter, bigPlant, trashBin,
    wallWindow, posterFrame, plank, clockFace, disc,
  };
})();

window.KilnProps = KilnProps;
