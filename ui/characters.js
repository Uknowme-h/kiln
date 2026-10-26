/* Kiln's character cast.
 *
 * Each of the seven agents is a recipe compiled into a tiny set of frames
 * (idle, step-A, step-B) for a 16×24 body. The designs are ours and are built
 * so each role reads at a glance: the Requirements analyst is tall and robed,
 * the Planner is broad and aproned, the Test author is neat with a checklist,
 * the two Workers are sturdy bricklayers, the Fixer is wiry with a satchel,
 * the Reporter is tidy with a ledger. Nothing here is traced from any other
 * project.
 */
"use strict";

const KilnChars = (() => {
  const { makeBuf, px, rect, alpha, hex, shade, outline, toCanvas } = Kiln.px;
  const T = Kiln.T;
  const W = 16, H = 24, INK = hex(T.ink[900]);

  const SKIN = {
    light: { hi: [255, 221, 189], base: [244, 199, 168], sh: [209, 156, 124] },
    tan: { hi: [233, 183, 137], base: [212, 161, 115], sh: [174, 125, 85] },
    brown: { hi: [181, 131, 95], base: [157, 111, 77], sh: [123, 85, 57] },
    dark: { hi: [143, 99, 71], base: [119, 79, 55], sh: [93, 61, 41] },
  };

  // head occupies cols 4-11, rows 2-10. Face features sit inside that box.
  function head(buf, skin) {
    const s = SKIN[skin];
    for (let y = 3; y <= 10; y++) for (let x = 4; x <= 11; x++) px(buf, x, y, s.base);
    for (let y = 4; y < 8; y++) px(buf, 5, y, s.hi); // left light
    for (let y = 5; y <= 9; y++) px(buf, 10, y, s.sh); // right shade
    for (const x of [6, 7, 8, 9]) px(buf, x, 10, s.sh); // jaw shade
  }
  function face(buf, skin, mouth) {
    const pup = [46, 38, 42];
    for (const ex of [5, 10]) { px(buf, ex, 7, pup); px(buf, ex, 8, pup); } // eyes
    const m = { smile: [6, 7, 8, 9], flat: [6, 7, 8, 9], frown: [6, 7, 8, 9] }[mouth] || [6, 7, 8, 9];
    const my = mouth === "frown" ? 9 : 8;
    for (const x of m) px(buf, x, my, [158, 86, 80]);
  }

  // ── hair / hats ── each returns void, draws over the head box
  const hair = {
    short(buf, c) { const [hi, b] = shade(c, 1.2), sh = shade(c, 0.7);
      rect(buf, 4, 2, 11, 4, b); for (let x = 4; x <= 11; x++) px(buf, x, 2, hi); px(buf, 4, 5, b); px(buf, 11, 5, b); },
    hood(buf, c) { const b = c, sh = shade(c, 0.7);
      rect(buf, 3, 1, 12, 4, b); rect(buf, 3, 5, 3, 9, b); rect(buf, 12, 5, 12, 9, b);
      px(buf, 4, 2, shade(c, 1.2)); px(buf, 4, 5, sh); px(buf, 11, 5, sh); },
    cap(buf, c) { const b = c, sh = shade(c, 0.7);
      rect(buf, 4, 1, 11, 4, b); rect(buf, 4, 4, 13, 4, b); px(buf, 12, 4, sh); px(buf, 13, 4, sh); },
    bun(buf, c) { const [hi, b] = shade(c, 1.2);
      rect(buf, 4, 2, 11, 4, b); px(buf, 7, 1, b); px(buf, 8, 1, b); px(buf, 7, 0, b); px(buf, 8, 0, b);
      for (let x = 4; x <= 11; x++) px(buf, x, 2, hi); },
    messy(buf, c) { const b = c;
      rect(buf, 4, 2, 11, 4, b); for (const [x, y] of [[4,1],[6,0],[8,1],[10,0],[11,1]]) px(buf, x, y, b); px(buf, 4, 5, b); px(buf, 11, 5, b); },
    band(buf, c) { const b = c;
      rect(buf, 4, 2, 11, 4, b); px(buf, 4, 5, b); px(buf, 11, 5, b);
      rect(buf, 4, 3, 11, 3, shade(c, 1.3)); },
  };

  // ── bodies ── torso rows 11-19, legs 20-22, feet 23. build changes width.
  function legs(buf, pants, phase) {
    const sh = shade(pants, 0.7);
    rect(buf, 5, 20, 7, 22, pants); rect(buf, 8, 20, 10, 22, pants);
    px(buf, 8, 20, sh); px(buf, 8, 21, sh);
    const leftLow = phase !== 1, rightLow = phase !== 2;
    const shoe = [44, 40, 48];
    rect(buf, 5, leftLow ? 23 : 22, 7, leftLow ? 23 : 22, shoe);
    rect(buf, 8, rightLow ? 23 : 22, 10, rightLow ? 23 : 22, shoe);
  }
  function torso(buf, r) {
    const [hi, b, sh] = [shade(r.c1, 1.18), r.c1, shade(r.c1, 0.7)];
    const wide = r.build === "broad" || r.build === "heavy";
    const slim = r.build === "slim";
    const x0 = wide ? 2 : slim ? 4 : 3, x1 = wide ? 13 : slim ? 11 : 12;
    rect(buf, x0 + 1, 11, x1 - 1, 11, b); // shoulders
    rect(buf, x0, 12, x1, 19, b);
    for (let y = 13; y <= 19; y++) px(buf, x1, y, sh); // right shade
    px(buf, x0, 13, hi); // left light
    if (r.apron) { rect(buf, 5, 13, 10, 19, r.apron); rect(buf, 6, 11, 9, 12, r.apron); }
    if (r.belt) rect(buf, x0, 16, x1, 16, r.belt);
    if (r.sash) for (let y = 12; y <= 18; y++) px(buf, 4 + (y - 12), y, r.sash);
  }
  function accessory(buf, r) {
    if (r.scroll) { rect(buf, 12, 13, 14, 16, hex(T.cream[50])); px(buf, 13, 14, INK); px(buf, 13, 15, INK); }
    if (r.clip) { rect(buf, 12, 14, 14, 17, hex(T.world.woodDark)); rect(buf, 12, 13, 14, 13, hex(T.ink[500])); }
    if (r.check) { for (let y = 13; y <= 15; y++) px(buf, 13, y, INK); px(buf, 14, 15, INK); px(buf, 15, 12, INK); px(buf, 16, 13, INK); }
    if (r.trowel) { rect(buf, 13, 15, 15, 17, hex(T.ink[300])); px(buf, 14, 18, hex(T.world.woodDark)); }
    if (r.satchel) { rect(buf, 1, 14, 3, 17, hex(T.world.woodDark)); px(buf, 2, 15, hex(T.accent.lemon)); }
    if (r.ledger) { rect(buf, 12, 14, 15, 17, hex(T.accent.sky)); px(buf, 13, 15, hex(T.cream[50])); px(buf, 13, 16, hex(T.cream[50])); }
  }

  function compile(r, phase) {
    const buf = makeBuf(W, H);
    torso(buf, r);
    legs(buf, r.pants, phase);
    head(buf, r.skin);
    face(buf, r.skin, r.mouth);
    hair[r.hairStyle](buf, r.hairColor);
    accessory(buf, r);
    outline(buf, INK);
    return toCanvas(buf);
  }

  // ── the cast ──
  const A = T.accent;
  const RECIPES = {
    requirements: { skin: "light", hairStyle: "hood", hairColor: hex(A.lilac), build: "slim",
      c1: hex(A.lilac), pants: hex(T.ink[700]), mouth: "smile", scroll: true, sash: hex(T.cream[100]) },
    planner: { skin: "tan", hairStyle: "cap", hairColor: hex(A.peach), build: "broad",
      c1: hex(A.peach), pants: hex(T.ink[700]), mouth: "flat", apron: hex(T.cream[200]), belt: hex(T.ink[500]), clip: true },
    test_author: { skin: "light", hairStyle: "bun", hairColor: hex(A.sky), build: "slim",
      c1: hex(A.sky), pants: hex(T.ink[700]), mouth: "smile", apron: hex(T.cream[100]), check: true },
    "worker-1": { skin: "brown", hairStyle: "band", hairColor: hex(A.coral), build: "heavy",
      c1: hex(A.coral), pants: hex(T.ink[700]), mouth: "flat", belt: hex(T.ink[900]), trowel: true },
    "worker-2": { skin: "dark", hairStyle: "band", hairColor: hex(A.mint), build: "heavy",
      c1: hex(A.mint), pants: hex(T.ink[700]), mouth: "flat", belt: hex(T.ink[900]), trowel: true },
    fixer: { skin: "tan", hairStyle: "messy", hairColor: hex(A.lemon), build: "slim",
      c1: hex(A.lemon), pants: hex(T.ink[700]), mouth: "smile", satchel: true },
    reporter: { skin: "light", hairStyle: "short", hairColor: hex(A.mint), build: "slim",
      c1: hex(A.mint), pants: hex(T.ink[700]), mouth: "smile", apron: hex(T.cream[100]), ledger: true },
  };

  const cache = new Map();
  function frames(id) {
    let f = cache.get(id);
    if (!f) {
      const r = RECIPES[id] || RECIPES["worker-1"];
      f = { idle: compile(r, 0), stepA: compile(r, 1), stepB: compile(r, 2), accent: r.c1 };
      cache.set(id, f);
    }
    return f;
  }
  return { frames, W, H };
})();

window.KilnChars = KilnChars;
