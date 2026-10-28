/* The Kiln floor scene.
 *
 * A walled office with named rooms, in the spirit of office-floor agent
 * viewers: a meeting room (walls with door gaps, conference table, whiteboard),
 * plan desks, a test lab with the CI rack, the build floor bullpen, a docs
 * corner, and a break nook on wood flooring. Agents walk to stations when the
 * events call for it — the fixer walks to a failing worker's desk, the
 * requirements analyst walks to the whiteboard, the reporter walks to the
 * printer. Every visual is procedural; the wall clock shows real time.
 */
"use strict";

const KilnScene = (() => {
  const T = Kiln.T;
  const chars = window.KilnChars;
  const props = window.KilnProps;
  const { px, makeBuf, toCanvas, hex } = Kiln.px;
  const SCALE = 3;
  const WALL_H = 49; // top wall band + baseboard

  const seats = {
    requirements: { x: 500, y: 150 },
    planner: { x: 660, y: 150 },
    test_author: { x: 830, y: 150 },
    "worker-1": { x: 180, y: 355 },
    "worker-2": { x: 360, y: 355 },
    fixer: { x: 540, y: 355 },
    reporter: { x: 790, y: 360 },
  };
  const RACK_POS = { x: 905, y: 44 };
  const BOARD_POS = { x: 44, y: 246 };

  // Walking routes, each one a real office action:
  //   kickoff    — analyst + PM meet at the conference table when a run starts
  //   whiteboard — the analyst drafts the spec on the meeting-room whiteboard
  //   printer    — the reporter prints the run report
  //   help       — the fixer walks to a failing dev's desk to pair-debug
  // Agents leave their desk upward (behind it) and use corridors, so they
  // never cross furniture. goHome() retraces the accumulated route.
  const DOOR_OUT = { x: 470, y: 152 };
  const DOOR = { x: 398, y: 152 };
  const DOOR_IN = { x: 370, y: 150 };
  const TABLE_SPOTS = {
    requirements: { x: 190, y: 96 },
    planner: { x: 250, y: 98 },
  };
  const STATION_LEGS = {
    whiteboard: () => [{ x: 90, y: 96 }, { x: 90, y: 156 }],
    printer: (home) => [{ x: home.x, y: 300 }, { x: 865, y: 300 }, { x: 865, y: 440 }],
  };
  // The fixer stands in the gap beside the host's desk, reached via the
  // corridor above the build-floor desks.
  function helpRoute(home, host) {
    const gx = host.home.x + 90;
    return [{ x: home.x, y: 250 }, { x: gx, y: 250 }, { x: gx, y: 335 }];
  }

  const NOTE_COLORS = {
    "worker-1": T.accent.coral,
    "worker-2": T.accent.mint,
    fixer: T.accent.lemon,
  };

  function create(canvas) {
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = false;

    const art = {
      deskIdle: props.desk(),
      deskOn: props.desk(hex(T.accent.lemon)),
      plant: props.plant(),
      bigPlant: props.bigPlant(),
      rackIdle: props.rack("idle"),
      rackGreen: props.rack("green"),
      rackRed: props.rack("red"),
      board: props.board(),
      table: props.conferenceTable(),
      whiteboard: props.whiteboard(),
      shelf: props.bookshelf(),
      cabinet: props.fileCabinet(),
      printer: props.printerTable(),
      couch: props.couch(),
      cooler: props.waterCooler(),
      coffee: props.coffeeCounter(),
      trash: props.trashBin(),
      windowArt: props.wallWindow(),
      poster: props.posterFrame(),
      kilnSign: props.plank(48, 14),
      zoneSign: props.plank(40, 12),
      clockFace: props.clockFace(),
      bubbles: {
        thinking: props.bubble("thinking"),
        working: props.bubble("working"),
        stuck: props.bubble("stuck"),
        done: props.bubble("done"),
      },
    };

    const agents = new Map();
    const envelopes = [];
    const particles = [];
    const notes = []; // task sticky notes on the cork board
    let ci = "idle";
    let clockCache = { key: -1, canvas: null };

    function addAgent(ref) {
      const seat = seats[ref.id] || { x: 400, y: 400 };
      agents.set(ref.id, {
        ...ref, x: seat.x, y: seat.y, home: { ...seat },
        status: "idle", detail: "", task: null,
        bob: Math.random() * Math.PI * 2, hop: 0,
        route: [], outRoute: null,
      });
    }
    function agent(id) { return agents.get(id); }

    function setRoute(a, points) {
      a.route = points.map((p) => ({ ...p }));
      a.outRoute = points.map((p) => ({ ...p }));
    }
    // Continue a trip with more legs (e.g. from the kickoff table on to the
    // whiteboard) without teleporting or U-turning.
    function appendRoute(a, points) {
      a.route.push(...points.map((p) => ({ ...p })));
      a.outRoute = (a.outRoute || []).concat(points.map((p) => ({ ...p })));
    }

    function sparkle(x, y, color) {
      for (let i = 0; i < 6; i++)
        particles.push({ x, y, vx: (Math.random() - 0.5) * 1.4, vy: -Math.random() * 1.4 - 0.4,
          life: 1, color: color || T.accent.lemon });
    }

    const api = {
      addAgent,
      agent,
      setStatus(id, status, detail) {
        const a = agents.get(id);
        if (!a) return;
        a.status = status; a.detail = detail || "";
        if (status === "working") a.hop = 1;
        if (status === "done") sparkle(a.x, a.y - 30, T.accent.mint);
      },
      setTask(id, title) { const a = agents.get(id); if (a) { a.task = title; a.hop = 1; } },
      pinTask(id, agentId) {
        if (notes.length < 8) notes.push({ color: NOTE_COLORS[agentId] || T.accent.sky, done: false });
      },
      message(fromId, toId) {
        const f = agents.get(fromId), t = agents.get(toId);
        if (f && t) envelopes.push({ x: f.x, y: f.y - 34, tx: t.x, ty: t.y - 34, t: 0 });
      },
      // A note from the client: envelope flies in from off-screen to the cork
      // board and pins a lemon sticky next to the team task cards.
      clientNote() {
        envelopes.push({
          x: -20, y: canvas.height - 40,
          tx: BOARD_POS.x + 56, ty: BOARD_POS.y + 36, t: 0,
        });
        if (notes.length < 8) notes.push({ color: T.accent.lemon, done: false });
      },
      kickoff(id) {
        const a = agents.get(id), spot = TABLE_SPOTS[id];
        if (a && spot) setRoute(a, [DOOR_OUT, DOOR, DOOR_IN, { x: 340, y: 96 }, spot]);
      },
      goStation(id, name) {
        const a = agents.get(id);
        if (a && STATION_LEGS[name]) appendRoute(a, STATION_LEGS[name](a.home));
      },
      // Standup behaviour: walk to the sprint board, take your card, walk back.
      collectCard(id) {
        const a = agents.get(id);
        if (!a) return;
        const stand = { x: 175, y: 318 };
        const to = [{ x: a.home.x, y: 300 }, { x: stand.x, y: 300 }, { ...stand }];
        const back = to.slice(0, -1).reverse().map((p) => ({ ...p }));
        a.route = [...to, ...back, { ...a.home }];
      },
      goHelp(id, hostId) {
        const a = agents.get(id), host = agents.get(hostId);
        if (a && host) setRoute(a, helpRoute(a.home, host));
      },
      goHome(id) {
        const a = agents.get(id);
        if (!a) return;
        const back = (a.outRoute || []).slice(0, -1).reverse().map((p) => ({ ...p }));
        a.route = [...back, { ...a.home }];
        a.outRoute = null;
      },
      celebrate() {
        for (const a of agents.values()) { a.hop = 1; sparkle(a.x, a.y - 30, T.accent.lemon); }
      },
      testRun(green) {
        ci = green ? "green" : "red";
        if (green) for (const n of notes) n.done = true;
        sparkle(RACK_POS.x + 40, RACK_POS.y + 30, green ? T.accent.mint : T.accent.coral);
      },
      reset() {
        ci = "idle";
        notes.length = 0;
        for (const a of agents.values()) {
          a.status = "idle"; a.task = null;
          a.x = a.home.x; a.y = a.home.y;
          a.route = []; a.outRoute = null;
        }
      },
    };

    // ── static scenery ──
    function wall() {
      ctx.fillStyle = T.cream[300];
      ctx.fillRect(0, 0, canvas.width, WALL_H - 5);
      ctx.fillStyle = T.world.wall;
      ctx.fillRect(0, WALL_H - 5, canvas.width, 4);
      ctx.fillStyle = T.ink[900];
      ctx.fillRect(0, WALL_H - 1, canvas.width, 1);

      for (const x of [110, 420, 730]) ctx.drawImage(art.windowArt, x, 6, 64, 36);
      ctx.drawImage(art.poster, 580, 6, 56, 36);

      ctx.drawImage(art.kilnSign, 14, 8, 96, 28);
      ctx.font = "10px 'Press Start 2P', monospace";
      ctx.textAlign = "center";
      ctx.fillStyle = T.cream[50];
      ctx.fillText("KILN", 62, 28);

      drawClock();
    }

    function drawClock() {
      const d = new Date();
      const key = d.getHours() * 60 + d.getMinutes();
      if (clockCache.key !== key) {
        const buf = makeBuf(24, 24);
        buf.data.set(art.clockFace.data);
        const cx = 12, cy = 12;
        const minA = (d.getMinutes() / 60) * Math.PI * 2 - Math.PI / 2;
        const hrA = (((d.getHours() % 12) + d.getMinutes() / 60) / 12) * Math.PI * 2 - Math.PI / 2;
        for (let t = 0; t <= 7; t += 0.5) px(buf, Math.round(cx + Math.cos(minA) * t), Math.round(cy + Math.sin(minA) * t), hex(T.ink[900]));
        for (let t = 0; t <= 5; t += 0.5) px(buf, Math.round(cx + Math.cos(hrA) * t), Math.round(cy + Math.sin(hrA) * t), hex(T.accent.coral));
        px(buf, cx, cy, hex(T.ink[900]));
        clockCache = { key, canvas: toCanvas(buf) };
      }
      ctx.drawImage(clockCache.canvas, 265, 8, 48, 48);
    }

    function floorTiles() {
      let r = 0;
      for (let y = WALL_H; y < canvas.height; y += T.tile, r++)
        for (let tx = 0; tx * T.tile < canvas.width; tx++) {
          ctx.fillStyle = (tx + r) % 2 ? T.world.floorA : T.world.floorB;
          ctx.fillRect(tx * T.tile, y, T.tile, T.tile);
        }
    }

    // Per-room flooring, drawn over the base checker.
    function zoneFloors() {
      ctx.globalAlpha = 0.16; ctx.fillStyle = T.accent.mint; // meeting room
      ctx.fillRect(0, WALL_H, 404, 179);
      ctx.globalAlpha = 0.12; ctx.fillStyle = T.accent.sky; // test lab
      ctx.fillRect(760, WALL_H, 240, 176);
      ctx.globalAlpha = 0.15; ctx.fillStyle = T.world.woodLight; // docs corner
      ctx.fillRect(708, 240, 292, 185);
      ctx.globalAlpha = 1;
      // break nook: warm wood checker
      let r = 0;
      for (let y = 425; y < canvas.height; y += T.tile, r++)
        for (let x = 500; x < canvas.width; x += T.tile) {
          ctx.fillStyle = (Math.floor(x / T.tile) + r) % 2 ? T.world.woodLight : T.world.woodDark;
          ctx.fillRect(x, y, T.tile, T.tile);
        }
    }

    function rugs() {
      // meeting room rug under the conference table
      ctx.fillStyle = "#CBE0D2"; ctx.fillRect(160, 92, 190, 110);
      ctx.fillStyle = "#A8CDB4";
      ctx.fillRect(160, 92, 190, 4); ctx.fillRect(160, 198, 190, 4);
      ctx.fillRect(160, 92, 4, 110); ctx.fillRect(346, 92, 4, 110);
      // build floor rug
      ctx.fillStyle = T.world.rug; ctx.fillRect(40, 250, 600, 175);
      ctx.fillStyle = T.world.rugEdge;
      ctx.fillRect(40, 250, 600, 6); ctx.fillRect(40, 419, 600, 6);
      ctx.fillRect(40, 250, 6, 175); ctx.fillRect(634, 250, 6, 175);
    }

    // Room dividers: wall segments with door gaps, plus cubicle partitions.
    function walls() {
      const ink = T.ink[900], cream = T.cream[100], wood = T.world.wall;
      function v(x, y, h) {
        ctx.fillStyle = ink; ctx.fillRect(x - 2, y - 2, 16, h + 4);
        ctx.fillStyle = cream; ctx.fillRect(x, y, 12, h);
        ctx.fillStyle = wood; ctx.fillRect(x - 2, y - 5, 16, 5);
        ctx.fillStyle = ink; ctx.fillRect(x - 2, y - 6, 16, 1);
      }
      function hz(x, y, w) {
        ctx.fillStyle = ink; ctx.fillRect(x - 2, y - 2, w + 4, 16);
        ctx.fillStyle = cream; ctx.fillRect(x, y, w, 12);
        ctx.fillStyle = wood; ctx.fillRect(x - 2, y - 5, w + 4, 5);
        ctx.fillStyle = ink; ctx.fillRect(x - 2, y - 6, w + 4, 1);
      }
      // meeting room: right wall + bottom wall, each with a door gap
      v(392, 49, 81); v(392, 175, 53);
      hz(40, 228, 170); hz(260, 228, 144);
      ctx.fillStyle = "#E8D8B0"; // door thresholds
      ctx.fillRect(392, 130, 12, 45); ctx.fillRect(210, 228, 50, 12);
      // cubicle partitions between the plan desks and the test desk
      v(578, 118, 67); v(738, 118, 67);
      // docs divider with a doorway gap
      v(700, 260, 70); v(700, 370, 45);
    }

    function decals() {
      ctx.globalAlpha = 0.35;
      ctx.font = "13px 'Pixelify Sans', monospace";
      ctx.textAlign = "center";
      ctx.fillStyle = T.ink[500];
      ctx.fillText("build floor", 380, 266);
      ctx.fillText("docs", 935, 505);
      ctx.fillText("break nook", 695, 548);
      ctx.globalAlpha = 1;
    }

    function furniture() {
      // meeting room: whiteboard left, conference table right of it
      ctx.drawImage(art.whiteboard, 20, 58, 128, 88);
      ctx.drawImage(art.table, 170, 100, 160, 88);
      // build floor: sprint board, then pinned task notes
      ctx.drawImage(art.board, BOARD_POS.x, BOARD_POS.y, 112, 72);
      notes.forEach((n, i) => {
        const col = i % 4, row = Math.floor(i / 4);
        const nx = BOARD_POS.x + 12 + col * 24, ny = BOARD_POS.y + 12 + row * 26;
        ctx.fillStyle = n.color;
        ctx.fillRect(nx, ny, 16, 18);
        ctx.fillStyle = T.ink[900];
        ctx.fillRect(nx + 3, ny + 4, 10, 1); ctx.fillRect(nx + 3, ny + 7, 10, 1);
        if (n.done) {
          ctx.fillStyle = T.accent.mint;
          ctx.fillRect(nx + 3, ny + 12, 2, 2); ctx.fillRect(nx + 5, ny + 14, 2, 2);
          ctx.fillRect(nx + 7, ny + 12, 2, 2); ctx.fillRect(nx + 9, ny + 10, 2, 2); ctx.fillRect(nx + 11, ny + 8, 2, 2);
        }
      });
      // bins tucked against walls
      ctx.drawImage(art.trash, 20, 240, 20, 28);
      ctx.drawImage(art.trash, 975, 380, 20, 28);
      // test lab
      ctx.drawImage(ci === "green" ? art.rackGreen : ci === "red" ? art.rackRed : art.rackIdle,
        RACK_POS.x, RACK_POS.y, 80, 112);
      ctx.font = "11px 'Pixelify Sans', monospace";
      ctx.textAlign = "center";
      ctx.fillStyle = T.ink[500];
      ctx.fillText("CI", RACK_POS.x + 40, RACK_POS.y + 126);
      // docs corner
      ctx.drawImage(art.shelf, 887, 180, 96, 128);
      ctx.drawImage(art.cabinet, 899, 316, 72, 96);
      ctx.drawImage(art.printer, 905, 420, 88, 60);
      // break nook, grouped on the wood floor
      ctx.drawImage(art.cooler, 525, 440, 40, 80);
      ctx.drawImage(art.couch, 580, 440, 144, 72);
      ctx.drawImage(art.bigPlant, 740, 432, 48, 80);
      ctx.drawImage(art.coffee, 800, 445, 96, 52);
      // greenery
      ctx.drawImage(art.plant, 406, 56, 40, 52);
      ctx.drawImage(art.plant, 20, 478, 40, 52);
    }

    function zoneSigns() {
      for (const [label, cx] of [["MEETING", 190], ["PLAN", 580], ["TEST", 845]]) {
        ctx.fillStyle = T.ink[700];
        ctx.fillRect(cx - 22, WALL_H, 1, 4); ctx.fillRect(cx + 22, WALL_H, 1, 4);
        ctx.drawImage(art.zoneSign, cx - 40, WALL_H + 4, 80, 24);
        ctx.font = "8px 'Press Start 2P', monospace";
        ctx.textAlign = "center";
        ctx.fillStyle = T.ink[900];
        ctx.fillText(label, cx, WALL_H + 20);
      }
    }

    // ── agents ──
    function drawAgent(a, now) {
      const f = chars.frames(a.id);

      // movement: follow the route one waypoint at a time
      if (a.route.length) {
        const t = a.route[0];
        const dx = t.x - a.x, dy = t.y - a.y;
        const dist = Math.hypot(dx, dy);
        const step = 2.6;
        if (dist <= step) { a.x = t.x; a.y = t.y; a.route.shift(); }
        else { a.x += (dx / dist) * step; a.y += (dy / dist) * step; }
      }
      const walking = a.route.length > 0;
      const working = a.status === "working" && !walking;
      a.bob += walking ? 0.3 : working ? 0.25 : 0.06;
      const bobY = Math.round(Math.sin(a.bob) * (walking || working ? 2 : 1));
      if (a.hop > 0) a.hop = Math.max(0, a.hop - 0.04);
      const hopY = -Math.round(Math.sin(a.hop * Math.PI) * 8);

      const img = walking
        ? (Math.floor(now / 160) % 2 ? f.stepA : f.stepB)
        : working
          ? (Math.floor(now / 180) % 3 === 1 ? f.stepA : Math.floor(now / 180) % 3 === 2 ? f.stepB : f.idle)
          : f.idle;

      const w = chars.W * SCALE, h = chars.H * SCALE;
      // body at the current position
      const bodyX = Math.round(a.x - w / 2);
      const bodyY = Math.round(a.y - h + bobY + hopY);
      ctx.drawImage(img, bodyX, bodyY, w, h);

      // the desk stays home and glows only while its owner is seated and working
      const atHome = a.x === a.home.x && a.y === a.home.y && !walking;
      const on = a.status === "working" && atHome;
      const deskImg = on ? art.deskOn : art.deskIdle;
      const deskW = 48 * 3, deskH = 28 * 3;
      const deskX = Math.round(a.home.x - deskW / 2);
      const deskY = Math.round(a.home.y - 34);
      ctx.drawImage(deskImg, deskX, deskY, deskW, deskH);

      const bub = art.bubbles[a.status];
      if (bub && a.status !== "idle") ctx.drawImage(bub, Math.round(a.x - 10), Math.round(a.y - h - 26 + bobY + hopY), 20, 18);

      ctx.font = "600 12px 'Pixelify Sans', monospace";
      ctx.textAlign = "center";
      ctx.fillStyle = T.ink[900];
      ctx.fillText(a.label.split(" ")[0], a.home.x, deskY + deskH + 14);
      if (a.task) {
        ctx.font = "11px 'Pixelify Sans', monospace";
        ctx.fillStyle = T.ink[500];
        ctx.fillText(a.task, a.home.x, deskY + deskH + 28);
      }
    }

    function frame(now) {
      wall();
      floorTiles();
      zoneFloors();
      rugs();
      walls();
      decals();
      furniture();
      zoneSigns();

      const list = [...agents.values()].sort((a, b) => a.y - b.y);
      for (const a of list) drawAgent(a, now);

      for (let i = envelopes.length - 1; i >= 0; i--) {
        const e = envelopes[i];
        e.t += 0.02;
        if (e.t >= 1) { sparkle(e.tx, e.ty, T.accent.lemon); envelopes.splice(i, 1); continue; }
        const x = e.x + (e.tx - e.x) * e.t;
        const y = e.y + (e.ty - e.y) * e.t - Math.sin(e.t * Math.PI) * 36;
        ctx.fillStyle = T.accent.lemon;
        ctx.fillRect(Math.round(x - 6), Math.round(y - 4), 12, 8);
        ctx.strokeStyle = T.ink[900];
        ctx.strokeRect(Math.round(x - 6), Math.round(y - 4), 12, 8);
      }

      for (let i = particles.length - 1; i >= 0; i--) {
        const p = particles[i];
        p.x += p.vx; p.y += p.vy; p.vy += 0.06; p.life -= 0.03;
        if (p.life <= 0) { particles.splice(i, 1); continue; }
        ctx.globalAlpha = Math.max(0, p.life);
        ctx.fillStyle = p.color;
        ctx.fillRect(Math.round(p.x), Math.round(p.y), 2, 2);
        ctx.globalAlpha = 1;
      }
    }
    api.frame = frame;
    return api;
  }

  return { create };
})();

window.KilnScene = KilnScene;
