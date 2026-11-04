/* The Kiln floor scene.
 *
 * Floor plan, 1216×736. Desk columns line up across rooms, so the office
 * reads as one grid: x 450/634 is shared by the plan office and the build
 * floor, and x 846/1030 by the QA lab and the support desks.
 *
 *   ┌──────────── wall: signs, windows, the clock ─────────────────────────┐
 *   │ MEETING      ║ PLAN               ║ QA LAB                            │
 *   │ whiteboard   ║  Riley     Pat     ║  Tessa     Remy          [CI]     │
 *   │ table        ║                    ║                                   │
 *   └───┤door├─────╨────────┤door├─────╨────────┤door├────────────────────┘
 *   ▸ entrance ═══════════════ corridor (main walking lane) ═════════════════
 *   ┌ BUILD FLOOR: 6 shared desks ────────┐║  Felix      Rosa       [shelf]
 *   │ [board]   B1      B2      B3        │║                        [printer]
 *   │           B4      B5      B6        door ── break nook ─────────────────
 *   └─────────────────────────────────────┘║  cooler   couch      coffee
 *
 * Agents walk a network of lanes (corridor, aisles, the lane behind the top
 * desks) and use the shortest path between any two points, so they go around
 * furniture and through doors. Between jobs they get coffee, chat at the water
 * cooler, sit on the couch, read the sprint board, look around, or doze off.
 *
 * Workers share the six build desks. A new worker walks in from the entrance
 * and takes a free desk, or the desk of the worker who finished first, who
 * then walks out. Every visual is procedural; the clock shows real time.
 */
"use strict";

const KilnScene = (() => {
  const T = Kiln.T;
  const chars = window.KilnChars;
  const props = window.KilnProps;
  const { px, makeBuf, toCanvas, hex } = Kiln.px;
  const SCALE = 3;
  const WALL_H = 52;
  const BODY_W = chars.W * SCALE; // 48
  const BODY_H = chars.H * SCALE; // 72
  const DESK_W = 144, DESK_H = 84, DESK_RISE = 34; // a desk is drawn from seat.y - 34
  const LABEL_W = 168; // a desk's label lane: the desk plus a 12px margin each side

  // ── the grid ──
  const ROW = { top: 172, a: 410, b: 628 }; // seat y (an agent's feet)
  const LANE = { back: 124, hall: 304, b: 548 }; // walking lanes
  const COL = { b1: 266, b2: 450, b3: 634, r1: 846, r2: 1030 }; // desk centers
  const AISLE = { left: 161, m1: 358, m2: 542, right: 938 }; // between desk columns
  const RACK_POS = { x: 1118, y: 64 };
  const BOARD_POS = { x: 16, y: 330 };

  // ── the walking network ──
  const NODES = new Map(); // "x,y" -> {x, y}
  const EDGES = new Map(); // "x,y" -> ["x,y", ...]
  function nodeId(x, y) {
    const id = x + "," + y;
    if (!NODES.has(id)) { NODES.set(id, { x, y }); EDGES.set(id, []); }
    return id;
  }
  function lane(...points) {
    let prev = null;
    for (const [x, y] of points) {
      const id = nodeId(x, y);
      if (prev) { EDGES.get(prev).push(id); EDGES.get(id).push(prev); }
      prev = id;
    }
  }
  {
    const H = LANE.hall, B = LANE.b, K = LANE.back;
    // corridor, from the entrance on the left
    lane([-40, H], [AISLE.left, H], [COL.b1, H], [AISLE.m1, H], [COL.b2, H], [AISLE.m2, H],
      [COL.b3, H], [COL.r1, H], [AISLE.right, H], [COL.r2, H]);
    // the lane between the build-floor rows, through the door into the nook
    lane([AISLE.left, B], [COL.b1, B], [AISLE.m1, B], [COL.b2, B], [AISLE.m2, B], [COL.b3, B],
      [736, B], [COL.r1, B], [AISLE.right, B], [1080, B], [1164, B]);
    // aisles; the stops on them beside a desk are where the fixer pair-debugs
    lane([AISLE.left, H], [AISLE.left, ROW.a], [AISLE.left, 460], [AISLE.left, B], [AISLE.left, ROW.b]);
    lane([AISLE.m1, H], [AISLE.m1, ROW.a], [AISLE.m1, B], [AISLE.m1, ROW.b]);
    lane([AISLE.m2, H], [AISLE.m2, ROW.a], [AISLE.m2, B], [AISLE.m2, ROW.b]);
    lane([AISLE.right, H], [AISLE.right, B]);
    // desks are entered from behind: row A from the corridor, row B from lane B
    for (const x of [COL.b1, COL.b2, COL.b3, COL.r1, COL.r2]) lane([x, H], [x, ROW.a]);
    for (const x of [COL.b1, COL.b2, COL.b3]) lane([x, B], [x, ROW.b]);
    // meeting room: door, then the table and the whiteboard
    lane([AISLE.left, H], [AISLE.left, 262], [AISLE.left, 226], [190, 226], [210, 226], [270, 226], [290, 226]);
    lane([AISLE.left, 226], [84, 196]);
    lane([210, 226], [240, 226], [270, 226]); // pacing room along the table, clear of the whiteboard
    // QA lab: along the front of the desks to the CI rack
    lane([AISLE.right, 262], [1158, 240]);
    // plan office and QA lab: up their door aisle to the lane behind the desks
    lane([AISLE.m2, H], [AISLE.m2, 262], [AISLE.m2, K], [COL.b2, K], [COL.b2, ROW.top]);
    lane([AISLE.m2, K], [COL.b3, K], [COL.b3, ROW.top]);
    lane([AISLE.right, H], [AISLE.right, 262], [AISLE.right, K], [COL.r1, K], [COL.r1, ROW.top]);
    lane([AISLE.right, K], [COL.r2, K], [COL.r2, ROW.top]);
    // sprint board, break nook
    lane([AISLE.left, 460], [72, 460]);
    lane([COL.r1, B], [COL.r1, 672], [943, 672], [999, 672]);
    lane([COL.r1, 672], [790, 708]);
    lane([1080, B], [1080, 656], [1140, 656]);
  }
  const EXIT = nodeId(-40, LANE.hall);

  const routeCache = new Map();
  function shortest(from, to) {
    if (from === to) return [from];
    const key = from + "|" + to;
    if (routeCache.has(key)) return routeCache.get(key);
    const dist = new Map([[from, 0]]), prev = new Map(), open = new Set([from]);
    while (open.size) {
      let u = null, best = Infinity;
      for (const k of open) { const d = dist.get(k); if (d < best) { best = d; u = k; } }
      open.delete(u);
      if (u === to) break;
      const pu = NODES.get(u);
      for (const v of EDGES.get(u)) {
        const pv = NODES.get(v);
        const nd = best + Math.hypot(pv.x - pu.x, pv.y - pu.y);
        if (nd < (dist.has(v) ? dist.get(v) : Infinity)) { dist.set(v, nd); prev.set(v, u); open.add(v); }
      }
    }
    let out = [from];
    if (prev.has(to)) {
      out = [to];
      for (let c = to; c !== from;) { c = prev.get(c); out.push(c); }
      out.reverse();
    }
    routeCache.set(key, out);
    return out;
  }
  function nearestNode(x, y) {
    let best = null, bd = Infinity;
    for (const [id, p] of NODES) {
      const d = (p.x - x) ** 2 + (p.y - y) ** 2;
      if (d < bd) { bd = d; best = id; }
    }
    return best;
  }

  // ── places ──
  // face: 1 looks right (the sprite as drawn), -1 is mirrored.
  const SPOTS = {
    table1: { at: "210,226", face: 1 },
    table2: { at: "270,226", face: -1 },
    whiteboard: { at: "84,196", face: -1, act: "write" },
    pace: { at: "240,226", face: -1, act: "pace" },
    rack: { at: "1158,240", face: 1, act: "read" },
    board: { at: "72,460", face: -1, act: "read" },
    printer: { at: "1164,548", face: 1, act: "print" },
    coffee: { at: "1140,656", face: 1, act: "coffee" },
    water: { at: "790,708", face: -1, act: "chat" },
    cooler: { at: "846,672", face: -1, act: "chat" },
    couch1: { at: "943,672", face: 1, act: "sit" },
    couch2: { at: "999,672", face: -1, act: "sit" },
  };
  // where idle agents like to go, and how much
  const IDLE_SPOTS = [["coffee", 3], ["cooler", 3], ["water", 2], ["couch1", 2], ["couch2", 2], ["board", 1]];

  // Seats. `help` is where a visitor stands to pair with whoever sits there.
  function seat(id, x, y, help, desk = true) {
    return { id, x, y, node: nodeId(x, y), help, desk, occupant: null, k: y + 0.5 };
  }
  const FIXED_SEATS = {
    requirements: seat("requirements", COL.b2, ROW.top, nodeId(COL.b2, LANE.back)),
    planner: seat("planner", COL.b3, ROW.top, nodeId(COL.b3, LANE.back)),
    test_author: seat("test_author", COL.r1, ROW.top, nodeId(COL.r1, LANE.back)),
    reviewer: seat("reviewer", COL.r2, ROW.top, nodeId(COL.r2, LANE.back)),
    fixer: seat("fixer", COL.r1, ROW.a, nodeId(COL.r1, LANE.hall)),
    reporter: seat("reporter", COL.r2, ROW.a, nodeId(COL.r2, LANE.hall)),
  };
  const BUILD_SEATS = [
    seat("b1", COL.b1, ROW.a, nodeId(AISLE.left, ROW.a)),
    seat("b2", COL.b2, ROW.a, nodeId(AISLE.m1, ROW.a)),
    seat("b3", COL.b3, ROW.a, nodeId(AISLE.m2, ROW.a)),
    seat("b4", COL.b1, ROW.b, nodeId(AISLE.left, ROW.b)),
    seat("b5", COL.b2, ROW.b, nodeId(AISLE.m1, ROW.b)),
    seat("b6", COL.b3, ROW.b, nodeId(AISLE.m2, ROW.b)),
  ];
  // when all six build desks are busy, extra workers take a laptop to the
  // ends of the meeting table (the middle stays free for the kickoff)
  const OVERFLOW_SEATS = [
    seat("o1", 190, 226, "190,226", false),
    seat("o2", 290, 226, "290,226", false),
  ];
  const OVERFLOW_LABEL_W = 92;
  const DESK_SEATS = [...Object.values(FIXED_SEATS), ...BUILD_SEATS];
  const ALL_SEATS = [...DESK_SEATS, ...OVERFLOW_SEATS];

  const WORKER_NOTE_COLORS = [
    T.accent.coral, T.accent.mint, T.accent.sky, T.accent.lemon, T.accent.lilac, T.accent.peach,
  ];
  function noteColorFor(id) {
    if (id === "fixer") return T.accent.lemon;
    const m = /^worker-(\d+)$/.exec(id);
    if (m) return WORKER_NOTE_COLORS[(parseInt(m[1], 10) - 1) % WORKER_NOTE_COLORS.length];
    return T.accent.lemon;
  }

  // Short desk label: "Worker 3" stays whole so parallel workers stay distinct.
  function shortLabel(label) {
    const s = String(label || "");
    return /^Worker \d+$/.test(s) ? s : s.split(" ")[0];
  }

  // The simulation runs on a fixed 60 Hz step, so walking, bobbing and
  // envelopes move at the same speed on a 60 Hz laptop and a 144 Hz monitor.
  const STEP_MS = 1000 / 60;
  const MAX_CATCHUP_MS = 250; // after a hidden tab, skip ahead instead of fast-forwarding
  const DIMMED_FRAME_MS = 1000 / 20; // behind an overlay the floor only needs ~20 fps
  const WALK = 2.6; // px per step on an errand
  const STROLL = 1.5; // px per step on a break
  const NAP_AFTER_MS = 45000;
  const DONE_BUBBLE_MS = 4000;

  const rand = (lo, hi) => lo + Math.random() * (hi - lo);
  const busy = (a) => a.status === "working" || a.status === "thinking" || a.status === "stuck";

  // ── time of day ──
  // 0 = full daylight, 1 = night. Dusk runs 18:00–20:00, dawn 06:00–07:30.
  function nightness(h) {
    if (h >= 20 || h < 6) return 1;
    if (h >= 18) return (h - 18) / 2;
    if (h < 7.5) return 1 - (h - 6) / 1.5;
    return 0;
  }
  const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
  // day → warm dusk → cool night, so evenings glow before they go blue
  function through(day, dusk, night, n) {
    return n <= 0.5 ? mix(day, dusk, n / 0.5) : mix(dusk, night, (n - 0.5) / 0.5);
  }
  const rgb = (c, a = 1) => `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${a})`;
  const AMBIENT = [[255, 255, 255], [255, 216, 184], [118, 124, 182]]; // multiplied over the floor
  const SKY_TOP = [[168, 216, 224], [233, 162, 122], [38, 42, 82]];
  const SKY_LOW = [[205, 235, 240], [245, 201, 155], [61, 66, 119]];
  // the wall windows' glass, in canvas pixels (the window sprite is drawn at 2×)
  const WINDOWS = [150, 488, 900, 1032].map((x) => ({ x: x + 4, y: 12, w: 56, h: 28 }));

  // a soft warm pool of light for desk lamps at night
  function lampGlow() {
    const c = document.createElement("canvas");
    c.width = 220; c.height = 150;
    const g = c.getContext("2d");
    const grad = g.createRadialGradient(110, 75, 4, 110, 75, 100);
    grad.addColorStop(0, "rgba(255, 196, 112, 0.6)");
    grad.addColorStop(1, "rgba(255, 196, 112, 0)");
    g.fillStyle = grad;
    g.scale(1, 0.68);
    g.fillRect(0, 0, 220, 220);
    return c;
  }

  // Each idle role has its habits; workers just like the break nook.
  const ROLE_IDLE = {
    requirements: [["whiteboard", 4], ["board", 2], ["coffee", 2], ["cooler", 1]],
    planner: [["board", 4], ["pace", 3], ["coffee", 2]],
    test_author: [["rack", 4], ["coffee", 2], ["cooler", 2]],
    reviewer: [["printer", 3], ["board", 3], ["coffee", 2]],
    reporter: [["printer", 4], ["couch1", 2], ["coffee", 2]],
    fixer: [["cooler", 3], ["water", 2], ["coffee", 2], ["couch2", 1]],
  };
  const CODE_COLORS = [T.accent.mint, T.accent.sky, T.accent.lemon, T.accent.lilac];

  function create(canvas) {
    // opaque: the background covers every pixel, so the browser can skip alpha compositing
    const ctx = canvas.getContext("2d", { alpha: false });
    ctx.imageSmoothingEnabled = false;

    const art = {
      deskIdle: props.desk(),
      deskOn: props.desk(hex(T.accent.lemon)),
      plant: props.plant(),
      bigPlant: props.bigPlant(),
      rackIdle: props.rack("idle"),
      rackGreen: props.rack("green"),
      rackRed: props.rack("red"),
      board: props.board(true),
      glow: lampGlow(),
      table: props.conferenceTable(),
      whiteboard: props.whiteboard(),
      shelf: props.bookshelf(),
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
      zee: props.zee(),
      doormat: props.doormat(),
      bubbles: {
        thinking: props.bubble("thinking"),
        working: props.bubble("working"),
        stuck: props.bubble("stuck"),
        done: props.bubble("done"),
        chat: props.bubble("chat"),
        ask: props.bubble("ask"),
      },
    };

    const agents = new Map();
    const order = []; // the agents, for the update loop
    const drawList = []; // agents and desks, depth-sorted in place each frame
    const roster = []; // refs from /api/agents, re-seated on reset()
    const envelopes = [];
    const particles = [];
    const notes = []; // kanban cards on the sprint board: {task, owner, color, state, x, y}
    const links = []; // dotted "talking to" lines between two agents: {from, to, t}
    let ci = "idle";
    let clockCache = { key: -1, canvas: null };
    let bg = null; // the static scenery, painted once onto an offscreen canvas
    let dimmed = false;
    let lastTick = null, acc = 0, lastDraw = 0, sim = 0;
    let font = "";
    let reduced = false; // prefers-reduced-motion: no idle wandering, bobbing, confetti
    let clockOverride = null; // hours 0–24 to preview a time of day (?hour=21)
    let focusId = null; // the agent open in the drawer gets a ring under their feet
    let rackFx = null; // {kind: "sweep" | "alarm", until}
    let fanA = 0; // ceiling-fan angle
    let steamT = 0;
    let scribble = 0; // how much of the spec is on the whiteboard, 0..1

    function setFont(f) {
      if (font !== f) { ctx.font = f; font = f; }
    }

    // ── seating ──
    function claimSeat(a) {
      const fixed = FIXED_SEATS[a.id];
      if (fixed && !fixed.occupant) return fixed;
      let s = BUILD_SEATS.find((x) => !x.occupant);
      if (!s) {
        // hot-desking: the worker who finished first hands over their desk
        let oldest = null;
        for (const x of BUILD_SEATS) {
          const o = agents.get(x.occupant);
          if (o && !busy(o) && (!oldest || o.statusAt < agents.get(oldest.occupant).statusAt)) oldest = x;
        }
        if (oldest) { leave(agents.get(oldest.occupant)); s = oldest; }
      }
      return s || OVERFLOW_SEATS.find((x) => !x.occupant) || OVERFLOW_SEATS[0];
    }

    function leave(a) {
      if (a.seat && a.seat.occupant === a.id) a.seat.occupant = null;
      a.seat = null;
      a.mode = "leaving";
      a.target = null;
      a.act = null; a.napping = false;
      walkTo(a, EXIT, WALK);
    }

    function addAgent(ref, dynamic) {
      if (!ref || !ref.id) return;
      if (agents.has(ref.id)) return; // keep existing seat / pose
      if (!dynamic && !roster.some((r) => r.id === ref.id)) roster.push({ ...ref });
      const a = {
        ...ref, short: shortLabel(ref.label), dynamic: !!dynamic,
        x: 0, y: 0, at: null, route: [], speed: WALK,
        seat: null, mode: "home", target: null, help: null, after: null, dwell: Infinity,
        status: "idle", statusAt: sim, detail: "", task: null, taskFit: null,
        bob: Math.random() * Math.PI * 2, hop: 0, face: 1,
        act: null, idleIn: rand(4000, 20000), idleFor: 0, napping: false,
        lookT: 0, phase: Math.random() * 4000, fxT: 0, k: 0, waiting: false,
      };
      agents.set(ref.id, a);
      order.push(a);
      const s = claimSeat(a);
      a.seat = s;
      s.occupant = a.id;
      const isWorker = /^worker-\d+$/.test(a.id);
      if (dynamic && isWorker) {
        // a new contractor walks in through the front door
        const p = NODES.get(EXIT);
        a.x = p.x; a.y = p.y; a.at = EXIT;
        goHomeNow(a);
      } else {
        a.x = s.x; a.y = s.y; a.at = s.node;
      }
    }

    // relabel: the event carries the authoritative AgentRef (a real run's
    // "Worker 1" replaces the rehearsal roster's "Wren Worker").
    function ensureAgent(ref, relabel) {
      if (!ref || !ref.id) return null;
      if (!agents.has(ref.id)) addAgent(ref, true);
      const a = agents.get(ref.id);
      if (relabel && ref.label && a.label !== ref.label) {
        a.label = ref.label;
        a.short = shortLabel(ref.label);
      }
      return a;
    }
    function agent(id) { return agents.get(id); }

    function removeAgent(a) {
      agents.delete(a.id);
      const i = order.indexOf(a);
      if (i >= 0) order.splice(i, 1);
    }

    // ── movement ──
    function walkTo(a, target, speed) {
      // mid-walk, finish the current leg first so nobody cuts through furniture
      const start = a.route.length ? a.route[0] : (a.at || nearestNode(a.x, a.y));
      const ids = shortest(start, target);
      a.route = a.route.length ? ids.slice() : ids.slice(1);
      a.speed = speed;
      a.act = null;
      a.napping = false;
      a.idleFor = 0;
      if (!a.route.length) arrive(a); // already there
    }

    function goSpot(a, name, mode, opts) {
      const spot = SPOTS[name];
      if (!a || !spot || a.mode === "leaving") return;
      a.mode = mode;
      a.target = name;
      a.help = null;
      a.after = (opts && opts.after) || null;
      a.dwell = opts && opts.dwell != null ? opts.dwell : Infinity;
      walkTo(a, spot.at, mode === "wander" ? STROLL : WALK);
    }

    function goHomeNow(a) {
      if (!a || !a.seat || a.mode === "leaving") return;
      a.mode = "returning";
      a.target = null;
      a.help = null;
      a.after = null;
      walkTo(a, a.seat.node, WALK);
    }

    function arrive(a) {
      if (a.mode === "leaving") { removeAgent(a); return; }
      if (a.mode === "returning" || (a.seat && a.at === a.seat.node && !a.target && !a.help)) {
        a.mode = "home";
        a.face = 1;
        a.idleIn = rand(8000, 25000);
        a.idleFor = 0;
        return;
      }
      if (a.help) {
        const host = agents.get(a.help);
        a.face = host && host.x < a.x ? -1 : 1;
        a.act = "chat"; // pair-debugging is a conversation
        return;
      }
      const spot = SPOTS[a.target];
      if (spot) { a.face = spot.face; a.act = spot.act || null; }
    }

    function spotTaken(name, except) {
      for (const o of agents.values()) if (o !== except && o.target === name) return true;
      return false;
    }

    // An idle agent at their desk decides what to do with the next few seconds.
    function idleChoice(a) {
      a.idleIn = rand(10000, 28000);
      if (reduced) { a.lookT = 1800; return; } // stays put; only turns to look
      const r = Math.random();
      if (r < 0.55) {
        const habits = ROLE_IDLE[a.id] || IDLE_SPOTS;
        const free = habits.filter(([n]) => !spotTaken(n, a));
        if (free.length) {
          let pick = Math.random() * free.reduce((s, [, w]) => s + w, 0);
          const [name] = free.find(([, w]) => (pick -= w) <= 0) || free[0];
          goSpot(a, name, "wander", { dwell: rand(5000, 12000), after: "home" });
          return;
        }
      }
      if (r < 0.8) a.lookT = 1800; // look around
      else { a.hop = 0.6; a.face = -a.face; } // stretch
    }

    // ── public API ──
    const api = {
      addAgent,
      ensureAgent,
      agent,
      // Hit test for clicks. A body wins over a desk, and of overlapping bodies
      // the front-most (lowest on screen, drawn last) wins. The desk and its
      // name label stay clickable while the agent is away on a break.
      agentAt(x, y) {
        let body = null, desk = null;
        for (const a of agents.values()) {
          const sitting = a.act === "sit" && !a.route.length;
          const top = a.y - (sitting ? 64 : BODY_H) - 10;
          if (Math.abs(x - a.x) <= BODY_W / 2 + 4 && y >= top && y <= a.y + 6) {
            if (!body || a.y > body.y) body = a;
          }
          const s = a.seat;
          if (s && Math.abs(x - s.x) <= (s.desk ? DESK_W / 2 : BODY_W / 2) + 4 &&
            y >= s.y - DESK_RISE && y <= s.y + (s.desk ? 82 : 20)) {
            if (!desk || s.y > desk.seat.y) desk = a;
          }
        }
        const hit = body || desk;
        return hit ? hit.id : null;
      },
      setStatus(id, status, detail) {
        const a = agents.get(id);
        if (!a) return;
        if (a.status !== status) a.statusAt = sim;
        a.status = status; a.detail = detail || "";
        a.napping = false; a.idleFor = 0;
        if (status === "working") a.hop = 1;
        if (status === "done") sparkle(a.x, a.y - 30, T.accent.mint);
        // the agent's cards move across the sprint board with their work
        if (status === "working" || status === "thinking") moveCards(id, "todo", "doing");
        else if (status === "done") { moveCards(id, "todo", "done"); moveCards(id, "doing", "done"); }
        // work calls: a break ends and the agent heads back to the desk
        if (busy(a) && a.mode === "wander") goHomeNow(a);
      },
      setTask(id, title) {
        const a = agents.get(id);
        if (a) { a.task = title; a.taskFit = null; a.hop = 1; }
      },
      pinTask(taskId, agentId) {
        if (notes.some((n) => n.task === taskId)) return;
        const a = agents.get(agentId);
        pin({ task: taskId, owner: agentId, color: noteColorFor(agentId), state: a && busy(a) ? "doing" : "todo" });
      },
      message(fromId, toId) {
        const f = agents.get(fromId), t = agents.get(toId);
        if (!f || !t) return;
        envelopes.push({ x: f.x, y: f.y - 34, tx: t.x, ty: t.y - 34, t: 0 });
        links.push({ from: fromId, to: toId, t: 0 });
        if (links.length > 10) links.shift();
      },
      // A note from the client: envelope flies in from off-screen to the cork
      // board and pins a lemon sticky in the to-do column.
      clientNote() {
        envelopes.push({ x: -20, y: canvas.height - 40, tx: BOARD_POS.x + 56, ty: BOARD_POS.y + 36, t: 0 });
        pin({ task: null, owner: "client", color: T.accent.lemon, state: "todo" });
      },
      // a command or question of this agent's waits on the user (null clears all)
      setWaiting(id, on) {
        if (id == null) { for (const a of agents.values()) a.waiting = false; return; }
        const a = agents.get(id);
        if (a) a.waiting = !!on;
      },
      setFocus(id) { focusId = id || null; },
      setReducedMotion(on) { reduced = !!on; },
      setClock(hours) { clockOverride = typeof hours === "number" && hours >= 0 && hours < 24 ? hours : null; },
      // kickoff: the analyst and the PM meet at the conference table
      kickoff(id) {
        const name = id === "planner" ? "table2" : "table1";
        goSpot(agents.get(id), name, "errand");
      },
      // whiteboard: the analyst drafts the spec · printer: the reporter prints the report
      goStation(id, name) { goSpot(agents.get(id), name, "errand"); },
      // standup: walk to the sprint board, take your card, back to your desk
      collectCard(id) { goSpot(agents.get(id), "board", "errand", { dwell: 1500, after: "home" }); },
      // the fixer walks over to a failing dev's desk to pair-debug
      goHelp(id, hostId) {
        const a = agents.get(id), host = agents.get(hostId);
        if (!a || !host || !host.seat || a === host || a.mode === "leaving") return;
        a.mode = "errand";
        a.target = null;
        a.after = null;
        a.help = host.id;
        walkTo(a, host.seat.help, WALK);
      },
      goHome(id) { goHomeNow(agents.get(id)); },
      // The run is over. Green: everyone hops, confetti falls, the rack's
      // lights sweep. Red: the rack blinks an alarm and the fixer jogs over.
      celebrate(green = true) {
        if (green) {
          for (const a of agents.values()) { if (!reduced) a.hop = 1; sparkle(a.x, a.y - 30, T.accent.lemon); }
          if (!reduced) confetti();
          for (const n of notes) n.state = "done";
          rackFx = { kind: "sweep", until: sim + 4000 };
        } else {
          rackFx = { kind: "alarm", until: sim + 6000 };
          goSpot(agents.get("fixer"), "rack", "errand", { dwell: 6000, after: "home" });
        }
      },
      testRun(green) {
        ci = green ? "green" : "red";
        if (green) for (const n of notes) n.state = "done";
        sparkle(RACK_POS.x + 40, RACK_POS.y + 30, green ? T.accent.mint : T.accent.coral);
      },
      reset() {
        ci = "idle";
        notes.length = 0;
        links.length = 0;
        envelopes.length = 0;
        particles.length = 0;
        rackFx = null;
        scribble = 0;
        // drop the previous run's extra workers; the roster is seated fresh
        agents.clear();
        order.length = 0;
        for (const s of ALL_SEATS) s.occupant = null;
        for (const ref of roster) addAgent(ref, false);
      },
      setDimmed(on) { dimmed = !!on; },
      // repaint the cached scenery, e.g. once the pixel fonts have loaded
      invalidate() { bg = null; },
    };

    function sparkle(x, y, color) {
      for (let i = 0; i < 6; i++)
        particles.push({ x, y, vx: (Math.random() - 0.5) * 1.4, vy: -Math.random() * 1.4 - 0.4,
          g: 0.06, fade: 0.03, life: 1, color: color || T.accent.lemon, sprite: null, w: 2, h: 2 });
    }

    function confetti() {
      const colors = [T.accent.coral, T.accent.mint, T.accent.sky, T.accent.lemon, T.accent.lilac, T.accent.peach];
      for (let i = 0; i < 140; i++)
        particles.push({
          x: rand(0, canvas.width), y: rand(-120, -4), vx: rand(-0.5, 0.5), vy: rand(0.6, 1.6),
          g: 0.012, fade: 0.0035, life: 1, color: colors[i % colors.length], sprite: null,
          w: 4, h: i % 3 ? 3 : 5,
        });
    }

    // ── the sprint board: three columns, cards slide between them ──
    const BOARD_COLS = { todo: 0, doing: 1, done: 2 };
    const CARD_W = 11, CARD_H = 12, CARDS_PER_COL = 6;
    function cardSlot(state, index) {
      const x0 = BOARD_POS.x + 8 + BOARD_COLS[state] * 32;
      return { x: x0 + 3 + (index % 2) * 14, y: BOARD_POS.y + 18 + Math.floor(index / 2) * 16 };
    }
    function pin(card) {
      // keep the board readable: the oldest finished cards come down first
      if (notes.length >= 18) {
        const i = notes.findIndex((n) => n.state === "done");
        notes.splice(i >= 0 ? i : 0, 1);
      }
      const slot = cardSlot(card.state, notes.filter((n) => n.state === card.state).length);
      notes.push({ ...card, x: slot.x, y: slot.y });
    }
    function moveCards(owner, from, to) {
      for (const n of notes) if (n.owner === owner && n.state === from) n.state = to;
    }

    // ── static scenery ──
    // Everything here is painted once into `bg` and blitted each frame; desks,
    // the clock, the CI rack, the sticky notes and the people are drawn live.
    function wallBand(g) {
      g.fillStyle = T.cream[300];
      g.fillRect(0, 0, canvas.width, WALL_H - 5);
      g.fillStyle = T.world.wall;
      g.fillRect(0, WALL_H - 5, canvas.width, 4);
      g.fillStyle = T.ink[900];
      g.fillRect(0, WALL_H - 1, canvas.width, 1);

      // each room's stretch of wall: its sign first, then a window or two
      g.drawImage(art.kilnSign, 16, 12, 96, 28);
      g.drawImage(art.windowArt, 150, 8, 64, 36);
      for (const [label, x] of [["PLAN", 366], ["QA LAB", 766]]) g.drawImage(art.zoneSign, x, 14, 80, 24);
      g.drawImage(art.windowArt, 488, 8, 64, 36);
      g.drawImage(art.poster, 612, 8, 56, 36);
      g.drawImage(art.windowArt, 900, 8, 64, 36);
      g.drawImage(art.windowArt, 1032, 8, 64, 36);

      g.textAlign = "center";
      g.font = "10px 'Press Start 2P', monospace";
      g.fillStyle = T.cream[50];
      g.fillText("KILN", 64, 32);
      g.font = "8px 'Press Start 2P', monospace";
      g.fillStyle = T.ink[900];
      g.fillText("PLAN", 406, 30);
      g.fillText("QA LAB", 806, 30);
    }

    function floorTiles(g) {
      let r = 0;
      for (let y = WALL_H; y < canvas.height; y += T.tile, r++)
        for (let tx = 0; tx * T.tile < canvas.width; tx++) {
          g.fillStyle = (tx + r) % 2 ? T.world.floorA : T.world.floorB;
          g.fillRect(tx * T.tile, y, T.tile, T.tile);
        }
    }

    // Per-room flooring, drawn over the base checker.
    function zoneFloors(g) {
      g.globalAlpha = 0.16; g.fillStyle = T.accent.mint; // meeting room
      g.fillRect(0, WALL_H, 336, 204);
      g.globalAlpha = 0.12; g.fillStyle = T.accent.sky; // QA lab
      g.fillRect(736, WALL_H, 480, 204);
      g.globalAlpha = 0.2; g.fillStyle = T.world.woodLight; // support desks + docs
      g.fillRect(750, 316, 466, 256);
      g.globalAlpha = 0.07; g.fillStyle = T.ink[900]; // the corridor runner
      g.fillRect(0, 272, canvas.width, 44);
      g.globalAlpha = 1;
      // break nook: a soft wood checker, so the room reads warm without shouting
      let r = 0;
      for (let y = 572; y < canvas.height; y += T.tile, r++)
        for (let x = 750, c = 0; x < canvas.width; x += T.tile, c++) {
          g.fillStyle = (c + r) % 2 ? T.world.woodLight : T.world.wood;
          g.fillRect(x, y, T.tile, T.tile);
        }
    }

    function rugs(g) {
      function rug(x, y, w, h, fill, edge, b) {
        g.fillStyle = fill; g.fillRect(x, y, w, h);
        g.fillStyle = edge;
        g.fillRect(x, y, w, b); g.fillRect(x, y + h - b, w, b);
        g.fillRect(x, y, b, h); g.fillRect(x + w - b, y, b, h);
      }
      rug(148, 92, 184, 112, "#CBE0D2", "#A8CDB4", 4); // under the conference table
      rug(16, 324, 704, 388, T.world.rug, T.world.rugEdge, 6); // the build floor
    }

    // Room dividers: wall segments with door gaps.
    function walls(g) {
      const ink = T.ink[900], cream = T.cream[100], wood = T.world.wall;
      function v(x, y, h) {
        g.fillStyle = ink; g.fillRect(x - 2, y - 2, 16, h + 4);
        g.fillStyle = cream; g.fillRect(x, y, 12, h);
        g.fillStyle = wood; g.fillRect(x - 2, y - 5, 16, 5);
        g.fillStyle = ink; g.fillRect(x - 2, y - 6, 16, 1);
      }
      function hz(x, y, w) {
        g.fillStyle = ink; g.fillRect(x - 2, y - 2, w + 4, 16);
        g.fillStyle = cream; g.fillRect(x, y, w, 12);
        g.fillStyle = wood; g.fillRect(x - 2, y - 5, w + 4, 5);
        g.fillStyle = ink; g.fillRect(x - 2, y - 6, w + 4, 1);
      }
      // the top rooms: side walls, then the corridor wall with a door per room,
      // each door centered on that room's aisle
      v(336, WALL_H, 204);
      v(736, WALL_H, 204);
      hz(0, 256, 131); hz(191, 256, 331); hz(562, 256, 356); hz(958, 256, 258);
      // the build floor's right wall, with a door into the break nook
      v(736, 316, 210); v(736, 576, 160);
      g.fillStyle = "#E8D8B0"; // thresholds
      g.fillRect(131, 256, 60, 12); g.fillRect(522, 256, 40, 12); g.fillRect(918, 256, 40, 12);
      g.fillRect(736, 526, 12, 50);
    }

    function decals(g) {
      g.globalAlpha = 0.35;
      g.font = "13px 'Pixelify Sans', monospace";
      g.textAlign = "center";
      g.fillStyle = T.ink[500];
      g.fillText("build floor", 72, 530);
      g.fillText("break nook", 1100, 724);
      g.globalAlpha = 1;
    }

    function furniture(g) {
      // meeting room
      g.drawImage(art.whiteboard, 20, 70, 128, 88);
      g.drawImage(art.table, 160, 100, 160, 88);
      // entrance
      g.drawImage(art.doormat, 0, 282, 20, 44);
      g.drawImage(art.plant, 1170, 268, 40, 52); // the corridor's far end
      // build floor: sprint board (its notes are drawn live), plant, bin
      g.drawImage(art.board, BOARD_POS.x, BOARD_POS.y, 112, 72);
      g.drawImage(art.plant, 34, 600, 40, 52);
      g.drawImage(art.trash, 92, 668, 20, 28);
      // QA lab: the rack is drawn live (its lights follow CI), its label here
      g.font = "11px 'Pixelify Sans', monospace";
      g.textAlign = "center";
      g.fillStyle = T.ink[500];
      g.fillText("CI", RACK_POS.x + 40, RACK_POS.y + 128);
      // docs wall
      g.drawImage(art.shelf, 1116, 326, 96, 128);
      g.drawImage(art.printer, 1120, 466, 88, 60);
      // break nook
      g.drawImage(art.cooler, 770, 600, 40, 80);
      g.drawImage(art.couch, 900, 612, 144, 72);
      g.drawImage(art.coffee, 1112, 588, 88, 48);
      g.drawImage(art.bigPlant, 1172, 650, 40, 70);
    }

    function buildBackground() {
      const c = document.createElement("canvas");
      c.width = canvas.width; c.height = canvas.height;
      const g = c.getContext("2d", { alpha: false });
      g.imageSmoothingEnabled = false;
      wallBand(g);
      floorTiles(g);
      zoneFloors(g);
      rugs(g);
      walls(g);
      decals(g);
      furniture(g);
      return c;
    }

    // ── live props ──
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
      ctx.drawImage(clockCache.canvas, 262, 2, 48, 48);
    }

    function hourNow() {
      if (clockOverride !== null) return clockOverride;
      const d = new Date();
      return d.getHours() + d.getMinutes() / 60;
    }

    // The windows' glass: the sky follows the clock — clouds drifting by day,
    // a warm dusk, stars at night.
    function drawWindows(n) {
      const top = rgb(through(...SKY_TOP, n)), low = rgb(through(...SKY_LOW, n));
      const drift = reduced ? 0 : sim / 90;
      for (let i = 0; i < WINDOWS.length; i++) {
        const w = WINDOWS[i];
        ctx.fillStyle = top; ctx.fillRect(w.x, w.y, w.w, 16);
        ctx.fillStyle = low; ctx.fillRect(w.x, w.y + 16, w.w, w.h - 16);
        ctx.save();
        ctx.beginPath(); ctx.rect(w.x, w.y, w.w, w.h); ctx.clip();
        if (n < 1) { // clouds, fading out with the light
          ctx.globalAlpha = 1 - n;
          ctx.fillStyle = T.cream[50];
          for (let c = 0; c < 2; c++) {
            const span = w.w + 30;
            const cx = w.x - 20 + ((drift * (c ? 0.6 : 1) + i * 37 + c * 41) % span);
            const cy = w.y + 5 + c * 12;
            ctx.fillRect(Math.round(cx), cy, 14, 4); ctx.fillRect(Math.round(cx) + 4, cy - 2, 6, 2);
          }
        }
        if (n > 0) { // stars
          ctx.globalAlpha = n;
          ctx.fillStyle = T.cream[50];
          for (let s = 0; s < 5; s++) {
            const sx = w.x + ((i * 53 + s * 23) % (w.w - 4)) + 2, sy = w.y + ((i * 17 + s * 11) % (w.h - 6)) + 2;
            const twinkle = reduced || (Math.floor(sim / 700) + s + i) % 5 ? 2 : 1;
            ctx.fillRect(sx, sy, twinkle, twinkle);
          }
        }
        ctx.restore();
        ctx.globalAlpha = 1;
        ctx.fillStyle = T.cream[50]; // mullions back on top of the sky
        ctx.fillRect(w.x + 26, w.y, 4, w.h);
        ctx.fillRect(w.x, w.y + 12, w.w, 4);
      }
    }

    function drawRack() {
      ctx.drawImage(ci === "green" ? art.rackGreen : ci === "red" ? art.rackRed : art.rackIdle,
        RACK_POS.x, RACK_POS.y, 80, 112);
      if (!rackFx) return;
      if (sim > rackFx.until) { rackFx = null; return; }
      // the four status LEDs on the rack face (sprite px 28–29, rows 10 + 12i, at 2×)
      const led = (i, color) => { ctx.fillStyle = color; ctx.fillRect(RACK_POS.x + 56, RACK_POS.y + 20 + i * 24, 4, 2); };
      if (rackFx.kind === "sweep") {
        const lit = reduced ? -1 : Math.floor(sim / 140) % 4;
        for (let i = 0; i < 4; i++) led(i, i === lit || reduced ? T.cream[50] : T.accent.mint);
        ctx.globalAlpha = 0.25; ctx.fillStyle = T.accent.mint;
        ctx.fillRect(RACK_POS.x - 4, RACK_POS.y - 4, 88, 120);
      } else {
        const on = reduced || Math.floor(sim / 300) % 2 === 0;
        for (let i = 0; i < 4; i++) led(i, on ? T.accent.coral : T.ink[700]);
        ctx.globalAlpha = on ? 0.3 : 0.1; ctx.fillStyle = T.accent.coral;
        ctx.fillRect(RACK_POS.x - 6, RACK_POS.y - 6, 92, 124);
      }
      ctx.globalAlpha = 1;
    }

    function drawBoard() {
      const counts = { todo: 0, doing: 0, done: 0 };
      for (const n of notes) {
        const i = counts[n.state]++;
        if (i >= CARDS_PER_COL) continue; // a full column shows its first six
        const x = Math.round(n.x), y = Math.round(n.y);
        ctx.fillStyle = T.ink[900]; ctx.fillRect(x - 1, y - 1, CARD_W + 2, CARD_H + 2);
        ctx.fillStyle = n.color; ctx.fillRect(x, y, CARD_W, CARD_H);
        ctx.fillStyle = T.ink[900];
        ctx.fillRect(x + 2, y + 3, CARD_W - 4, 1); ctx.fillRect(x + 2, y + 6, CARD_W - 5, 1);
        if (n.state === "done") { ctx.fillStyle = T.accent.mint; ctx.fillRect(x + 7, y + 8, 3, 3); }
        if (n.owner === "client") { ctx.fillStyle = T.accent.coral; ctx.fillRect(x + CARD_W - 3, y - 1, 3, 3); }
      }
    }

    // While someone drafts at the whiteboard, the spec appears line by line.
    function drawWhiteboard() {
      if (scribble <= 0) return;
      const lines = [[34, 116, 96], [34, 120, 70], [34, 124, 84], [34, 128, 52]];
      let left = scribble * lines.reduce((s, l) => s + l[2], 0);
      ctx.fillStyle = T.ink[700];
      for (const [x, y, w] of lines) {
        if (left <= 0) break;
        ctx.fillRect(x, y, Math.min(w, left), 2);
        left -= w;
      }
    }

    // A page slides out of the printer while someone is at it.
    function drawPrinter() {
      let printing = false;
      for (const a of order) if (a.act === "print" && !a.route.length) { printing = true; break; }
      if (!printing) return;
      const t = reduced ? 1 : (sim % 1600) / 1600;
      const h = Math.round(4 + 14 * t);
      ctx.fillStyle = T.ink[900]; ctx.fillRect(1149, 489, 30, h + 2);
      ctx.fillStyle = T.cream[50]; ctx.fillRect(1150, 490, 28, h);
      ctx.fillStyle = T.ink[300]; ctx.fillRect(1154, 492, 18, 1); if (h > 8) ctx.fillRect(1154, 496, 14, 1);
    }

    // A slow ceiling fan over the build floor, seen as its shadow.
    function drawFan() {
      ctx.save();
      ctx.globalAlpha = 0.055;
      ctx.fillStyle = T.ink[900];
      ctx.translate(COL.b2, 520);
      ctx.scale(1, 0.45);
      ctx.rotate(fanA);
      // five blades: four read as a big "X" on the rug
      for (let i = 0; i < 5; i++) { ctx.rotate((Math.PI * 2) / 5); ctx.fillRect(10, -6, 56, 12); ctx.fillRect(60, -8, 8, 16); }
      ctx.beginPath(); ctx.arc(0, 0, 11, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    }

    function drawLiveProps() {
      drawClock();
      drawRack();
      drawBoard();
      drawWhiteboard();
      drawPrinter();
      drawFan();
    }

    // ── simulation (fixed step) ──
    function updateAgent(a) {
      // movement: follow the route one node at a time
      if (a.route.length) {
        const t = NODES.get(a.route[0]);
        const dx = t.x - a.x, dy = t.y - a.y;
        const dist = Math.hypot(dx, dy);
        if (Math.abs(dx) > 0.5) a.face = dx < 0 ? -1 : 1;
        if (dist <= a.speed) {
          a.x = t.x; a.y = t.y; a.at = a.route.shift();
          if (!a.route.length) { arrive(a); if (!agents.has(a.id)) return; }
        } else {
          a.x += (dx / dist) * a.speed; a.y += (dy / dist) * a.speed;
        }
      }
      const walking = a.route.length > 0;
      const working = a.status === "working" && !walking;
      a.bob += walking ? 0.3 * (a.speed / WALK) : working ? 0.25 : a.napping ? 0.03 : 0.06;
      if (a.hop > 0) a.hop = Math.max(0, a.hop - 0.04);
      if (a.lookT > 0) {
        a.lookT -= STEP_MS;
        a.face = a.lookT > 0 && Math.floor(a.lookT / 600) % 2 ? -1 : 1;
      }
      if (walking) return;

      // a break, an errand with a timer, or the desk
      if (a.dwell !== Infinity && (a.target || a.help)) {
        a.dwell -= STEP_MS;
        if (a.dwell <= 0) { a.dwell = Infinity; if (a.after === "home") goHomeNow(a); }
      } else if (a.mode === "home") {
        if (busy(a)) { a.idleFor = 0; a.napping = false; }
        else {
          a.idleFor += STEP_MS;
          a.idleIn -= STEP_MS;
          if (a.idleFor > NAP_AFTER_MS) a.napping = true;
          if (a.idleIn <= 0 && !a.napping) idleChoice(a);
          else if (a.napping && a.idleIn <= -20000) { a.napping = false; a.idleFor = 0; idleChoice(a); }
        }
      }

      // little effects: steam off the coffee, z's off a nap
      a.fxT -= STEP_MS;
      if (a.fxT <= 0) {
        if (a.act === "coffee") {
          if (!reduced) particles.push({ x: a.x + 15 * a.face, y: a.y - 40, vx: (Math.random() - 0.5) * 0.2, vy: -0.35,
            g: 0, fade: 0.02, life: 1, color: T.cream[50], sprite: null, w: 2, h: 2 });
          a.fxT = 450;
        } else if (a.napping) {
          particles.push({ x: a.x + 10, y: a.y - BODY_H - 4, vx: reduced ? 0 : 0.18, vy: reduced ? -0.15 : -0.3,
            g: 0, fade: 0.008, life: 1, color: null, sprite: art.zee });
          a.fxT = 1500;
        } else a.fxT = 300;
      }
    }

    function update() {
      sim += STEP_MS;
      for (let i = order.length - 1; i >= 0; i--) updateAgent(order[i]);

      if (!reduced) fanA += 0.03;
      // the spec grows on the whiteboard while someone writes there (~40s to fill)
      for (const a of order) if (a.act === "write" && !a.route.length) { scribble = Math.min(1, scribble + STEP_MS / 40000); break; }
      // steam off the coffee machine
      steamT -= STEP_MS;
      if (steamT <= 0) {
        steamT = 900;
        if (!reduced) particles.push({ x: 1130 + rand(-2, 2), y: 586, vx: rand(-0.1, 0.1), vy: -0.3,
          g: 0, fade: 0.012, life: 0.8, color: T.cream[50], sprite: null, w: 2, h: 2 });
      }
      for (let i = links.length - 1; i >= 0; i--) {
        links[i].t += STEP_MS / 2600;
        if (links[i].t >= 1) links.splice(i, 1);
      }
      // cards slide to their column slot
      const counts = { todo: 0, doing: 0, done: 0 };
      for (const n of notes) {
        const slot = cardSlot(n.state, counts[n.state]++);
        n.x += (slot.x - n.x) * 0.12;
        n.y += (slot.y - n.y) * 0.12;
      }

      for (let i = envelopes.length - 1; i >= 0; i--) {
        const e = envelopes[i];
        e.t += 0.02;
        if (e.t >= 1) { sparkle(e.tx, e.ty, T.accent.lemon); envelopes.splice(i, 1); }
      }

      for (let i = particles.length - 1; i >= 0; i--) {
        const p = particles[i];
        p.x += p.vx; p.y += p.vy; p.vy += p.g; p.life -= p.fade;
        if (p.life <= 0) particles.splice(i, 1);
      }
    }

    // ── drawing ──
    function blit(img, sh, dx, dy, dh, flip) {
      if (!flip) { ctx.drawImage(img, 0, 0, chars.W, sh, dx, dy, BODY_W, dh); return; }
      ctx.save();
      ctx.translate(dx + BODY_W, dy);
      ctx.scale(-1, 1);
      ctx.drawImage(img, 0, 0, chars.W, sh, 0, 0, BODY_W, dh);
      ctx.restore();
    }

    function poseOffset(a) {
      if (reduced) return a.napping ? 3 : 0;
      const walking = a.route.length > 0;
      const working = a.status === "working" && !walking;
      const bobY = Math.round(Math.sin(a.bob) * (walking || working ? 2 : 1));
      const hopY = -Math.round(Math.sin(a.hop * Math.PI) * 8);
      return bobY + hopY + (a.napping ? 3 : 0);
    }

    // Pacing is drawn, not walked: a slow back-and-forth around the spot.
    function paceOffset(a) {
      if (a.act !== "pace" || a.route.length || reduced) return 0;
      return Math.round(Math.sin(sim / 900 + a.phase) * 24);
    }

    function drawAgent(a, now) {
      const f = chars.frames(a.id);
      const pace = paceOffset(a);
      if (pace) a.face = Math.cos(sim / 900 + a.phase) >= 0 ? 1 : -1;
      const walking = a.route.length > 0 || !!pace;
      const working = a.status === "working" && !walking;
      const off = poseOffset(a);
      const img = walking
        ? (Math.floor(now / (a.speed < WALK || pace ? 220 : 160)) % 2 ? f.stepA : f.stepB)
        : working && !reduced
          ? (Math.floor(now / 180) % 3 === 1 ? f.stepA : Math.floor(now / 180) % 3 === 2 ? f.stepB : f.idle)
          : f.idle;
      const x = Math.round(a.x + pace - BODY_W / 2);
      if (a.act === "sit" && !walking) {
        // on the couch: head and torso only, the cushions hide the legs
        blit(f.idle, 20, x, Math.round(a.y - 4 - 60 + off), 60, a.face < 0);
      } else {
        blit(img, chars.H, x, Math.round(a.y - BODY_H + off), BODY_H, a.face < 0);
      }
      if (a.act === "coffee" && !walking) {
        const mx = Math.round(a.x + 12 * a.face - 4), my = Math.round(a.y - 36 + off);
        ctx.fillStyle = T.ink[900]; ctx.fillRect(mx - 1, my - 1, 10, 11);
        ctx.fillStyle = T.accent.coral; ctx.fillRect(mx, my, 8, 9);
        ctx.fillStyle = T.cream[50]; ctx.fillRect(mx + 1, my + 1, 6, 2);
      }
    }

    // whoever is at their desk and busy, so the monitor (and the lamp) are on
    function deskState(s) {
      const o = s.occupant && agents.get(s.occupant);
      return o && busy(o) && o.mode === "home" && !o.route.length ? o.status : null;
    }

    // The monitor tells you at a glance what is happening at a desk: code
    // scrolling while working, a blinking cursor while thinking, a red
    // flicker when stuck.
    const seedOf = (s) => s.x * 7 + s.y * 13;
    function drawMonitor(s, state) {
      const mx = s.x - 12, my = s.y - DESK_RISE + 12, mw = 27, mh = 21; // the sprite's screen, at 3×
      if (state === "stuck") {
        const flick = reduced || Math.floor(sim / 130) % 4 !== 0;
        ctx.fillStyle = flick ? T.accent.coral : "#8C2F2A";
        ctx.fillRect(mx, my, mw, mh);
        ctx.fillStyle = T.cream[50];
        ctx.fillRect(mx + 12, my + 4, 3, 9); ctx.fillRect(mx + 12, my + 15, 3, 3);
        return;
      }
      ctx.fillStyle = "#2B2440";
      ctx.fillRect(mx, my, mw, mh);
      if (state === "thinking") {
        if (reduced || sim % 1000 < 550) { ctx.fillStyle = T.accent.lemon; ctx.fillRect(mx + 3, my + 3, 3, 6); }
        return;
      }
      const step = reduced ? 0 : Math.floor(sim / 240);
      const seed = seedOf(s);
      for (let i = 0; i < 4; i++) {
        const k = i + step + seed;
        const indent = (k % 3) * 3;
        const w = 5 + ((k * 9301 + 49297) % 233280) % 15;
        ctx.fillStyle = CODE_COLORS[(k * 7) % CODE_COLORS.length];
        ctx.fillRect(mx + 2 + indent, my + 2 + i * 5, Math.min(w, mw - 4 - indent), 2);
      }
    }

    function drawDesk(s) {
      ctx.drawImage(art.deskIdle, s.x - DESK_W / 2, s.y - DESK_RISE, DESK_W, DESK_H);
      const state = deskState(s);
      if (state) drawMonitor(s, state);
    }

    // Talking-to lines: a marching dotted line from sender to receiver, fading.
    function drawLinks() {
      if (!links.length) return;
      ctx.save();
      ctx.lineWidth = 2;
      ctx.setLineDash([4, 6]);
      ctx.lineDashOffset = reduced ? 0 : -sim / 25;
      ctx.strokeStyle = T.accent.lemon;
      for (const l of links) {
        const f = agents.get(l.from), t = agents.get(l.to);
        if (!f || !t) continue;
        ctx.globalAlpha = 0.9 * (1 - l.t);
        ctx.beginPath();
        ctx.moveTo(Math.round(f.x), Math.round(f.y - 40));
        ctx.lineTo(Math.round(t.x), Math.round(t.y - 40));
        ctx.stroke();
        ctx.fillStyle = T.accent.lemon;
        ctx.fillRect(Math.round(t.x) - 3, Math.round(t.y - 43), 6, 6);
      }
      ctx.restore();
    }

    // The agent open in the drawer: a soft ring under their feet.
    function drawFocus() {
      const a = focusId && agents.get(focusId);
      if (!a) return;
      const pulse = reduced ? 0 : Math.sin(sim / 260) * 2;
      const x = a.x + paceOffset(a);
      ctx.save();
      ctx.globalAlpha = 0.3;
      ctx.fillStyle = T.accent.lemon;
      ctx.beginPath(); ctx.ellipse(x, a.y + 1, 30 + pulse, 10 + pulse / 2, 0, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 0.9;
      ctx.lineWidth = 2;
      ctx.strokeStyle = T.accent.lemon;
      ctx.stroke();
      ctx.restore();
    }

    // Evening and night: the whole floor is tinted, and busy desks light up.
    function drawLighting(n) {
      if (n <= 0) return;
      ctx.save();
      ctx.globalCompositeOperation = "multiply";
      ctx.fillStyle = rgb(through(...AMBIENT, n));
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.globalCompositeOperation = "lighter";
      ctx.globalAlpha = Math.min(1, n * 1.2) * 0.55;
      for (const s of DESK_SEATS) {
        if (deskState(s)) ctx.drawImage(art.glow, s.x - 110, s.y - DESK_RISE - 40);
      }
      ctx.restore();
    }

    function fitText(text, max) {
      if (ctx.measureText(text).width <= max) return text;
      let lo = 0, hi = text.length;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (ctx.measureText(text.slice(0, mid) + "…").width <= max) lo = mid; else hi = mid - 1;
      }
      return text.slice(0, lo) + "…";
    }

    // Name and task sit on top of everything, so a passer-by never hides them.
    // After dark they switch to light ink to stay readable on the dim floor.
    function drawLabels(n) {
      const dark = n > 0.6;
      const nameInk = dark ? T.cream[50] : T.ink[900], taskInk = dark ? T.ink[100] : T.ink[500];
      for (const s of ALL_SEATS) {
        const a = s.occupant && agents.get(s.occupant);
        if (!a) continue;
        setFont("600 12px 'Pixelify Sans', monospace");
        ctx.fillStyle = nameInk;
        if (!s.desk) {
          // a laptop seat at the table: no room for the task line under the name
          ctx.fillText(fitText(a.short, OVERFLOW_LABEL_W), s.x, s.y + 14);
          continue;
        }
        const base = s.y - DESK_RISE + DESK_H + 14;
        ctx.fillText(a.short, s.x, base);
        if (a.task) {
          setFont("11px 'Pixelify Sans', monospace");
          if (a.taskFit === null) a.taskFit = fitText(a.task, LABEL_W);
          ctx.fillStyle = taskInk;
          ctx.fillText(a.taskFit, s.x, base + 14);
        }
      }
    }

    function talking(a) {
      if (!a.help && a.act !== "chat" && a.act !== "sit") return false;
      for (const o of agents.values()) {
        if (o === a || o.route.length) continue;
        if ((o.act === "chat" || o.act === "sit" || o.help === a.id || a.help === o.id) &&
          Math.abs(o.x - a.x) < 140 && Math.abs(o.y - a.y) < 90) return true;
      }
      return !!a.help; // pair-debugging talks even when the host is away
    }

    function drawBubbles() {
      for (const a of order) {
        let bub = null, lift = 0;
        if (busy(a)) bub = art.bubbles[a.status];
        else if (a.status === "done" && sim - a.statusAt < DONE_BUBBLE_MS) bub = art.bubbles.done;
        if (!a.route.length && talking(a) && (sim + a.phase) % 3200 < 1300) bub = art.bubbles.chat;
        if (a.waiting) { // waiting on you outranks everything, and bounces to be seen
          bub = art.bubbles.ask;
          lift = reduced ? 0 : -Math.round(Math.abs(Math.sin(sim / 260)) * 4);
        }
        if (!bub) continue;
        const top = a.act === "sit" && !a.route.length ? a.y - 64 : a.y - BODY_H;
        ctx.drawImage(bub, Math.round(a.x + paceOffset(a) - 10), Math.round(top - 26 + poseOffset(a) + lift), 20, 18);
      }
    }

    function draw(now) {
      if (!bg) bg = buildBackground();
      const n = nightness(hourNow());
      ctx.drawImage(bg, 0, 0);
      drawLiveProps();
      drawFocus();

      // depth: desks sit half a pixel in front of their own seat, so a seated
      // body is drawn first and the desk covers the legs
      drawList.length = 0;
      for (const s of DESK_SEATS) drawList.push(s);
      for (const a of order) { a.k = a.y; drawList.push(a); }
      drawList.sort((p, q) => p.k - q.k);
      ctx.textAlign = "center";
      for (const item of drawList) {
        if (item.desk !== undefined) drawDesk(item);
        else drawAgent(item, now);
      }

      // light last over the world, then the sky (the light source) and the
      // overlays, which stay readable at any hour
      drawLighting(n);
      drawWindows(n);
      drawLinks();
      drawLabels(n);
      drawBubbles();

      if (envelopes.length) {
        ctx.fillStyle = T.accent.lemon;
        ctx.strokeStyle = T.ink[900];
        for (const e of envelopes) {
          const x = e.x + (e.tx - e.x) * e.t;
          const y = e.y + (e.ty - e.y) * e.t - Math.sin(e.t * Math.PI) * 36;
          ctx.fillRect(Math.round(x - 6), Math.round(y - 4), 12, 8);
          ctx.strokeRect(Math.round(x - 6), Math.round(y - 4), 12, 8);
        }
      }

      for (const p of particles) {
        ctx.globalAlpha = Math.max(0, Math.min(1, p.life));
        if (p.sprite) ctx.drawImage(p.sprite, Math.round(p.x), Math.round(p.y), 15, 15);
        else { ctx.fillStyle = p.color; ctx.fillRect(Math.round(p.x), Math.round(p.y), p.w || 2, p.h || 2); }
      }
      ctx.globalAlpha = 1;

      // Dimming in-canvas: a CSS filter on a canvas that repaints every frame
      // makes the compositor re-filter the whole surface each time.
      if (dimmed) {
        ctx.fillStyle = "rgba(26, 19, 32, 0.45)";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
      }
    }

    function frame(now) {
      if (lastTick === null) lastTick = now;
      acc += Math.min(now - lastTick, MAX_CATCHUP_MS);
      lastTick = now;
      while (acc >= STEP_MS) { update(); acc -= STEP_MS; }
      if (dimmed && now - lastDraw < DIMMED_FRAME_MS) return;
      lastDraw = now;
      draw(now);
    }

    // Scenery text is baked into `bg`; repaint it once the pixel fonts arrive.
    if (document.fonts) {
      document.fonts.ready.then(() => { bg = null; });
      document.fonts.addEventListener("loadingdone", () => { bg = null; });
    }

    api.frame = frame;
    return api;
  }

  return { create };
})();

window.KilnScene = KilnScene;
