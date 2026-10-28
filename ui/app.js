/* Glue: load the roster, subscribe to the SSE event contract, drive the scene,
 * and run the client console: Brief (idle) → Pairing (waiting for the local
 * companion) → Connected (notes live against the real run). The header demo
 * button still plays the scripted rehearsal feed: running → Handoff. */
"use strict";

const canvas = document.getElementById("floor");
const scene = KilnScene.create(canvas);
const logEl = document.getElementById("log");
const connEl = document.getElementById("conn");
const meterFill = document.getElementById("meter-fill");
const meterLabel = document.getElementById("meter-label");
const ciEl = document.getElementById("ci");
const demoBtn = document.getElementById("demo-btn");

const stageWrap = document.querySelector(".stage-wrap");
const briefEl = document.getElementById("brief");
const ideaInput = document.getElementById("idea-input");
const briefBtn = document.getElementById("brief-btn");
const dockEl = document.getElementById("note-dock");
const noteInput = document.getElementById("note-input");
const pairEl = document.getElementById("pairing");
const pairCodeEl = document.getElementById("pair-code");
const pairCmdEl = document.getElementById("pair-cmd");
const pairStatusEl = document.getElementById("pair-status");
const copyCmdBtn = document.getElementById("copy-cmd");
const handoffEl = document.getElementById("handoff");
const handoffTitle = document.getElementById("handoff-title");
const handoffStats = document.getElementById("handoff-stats");
const againBtn = document.getElementById("again-btn");

// ── console state machine ──
let consoleState = "idle"; // idle | pairing | connected | running | finished
let lastTests = null; // last test_run_finished event
let lastUsage = null; // last usage_tick event
let lastIdea = "";
let activeRun = null; // the real run from POST /runs, while pairing/connected
let pollTimer = null;

function setConsole(state) {
  consoleState = state;
  briefEl.classList.toggle("hidden", state !== "idle");
  pairEl.classList.toggle("hidden", state !== "pairing");
  handoffEl.classList.toggle("hidden", state !== "finished");
  dockEl.classList.toggle("hidden", state !== "running" && state !== "connected");
  stageWrap.classList.toggle("dimmed", state !== "running" && state !== "connected");
}

function log(text, you) {
  const li = document.createElement("li");
  li.textContent = text;
  if (you) li.className = "you";
  logEl.prepend(li);
  while (logEl.children.length > 60) logEl.lastChild.remove();
}

function setConn(state) {
  connEl.className = "chip";
  if (state === "live") { connEl.classList.add("live"); connEl.textContent = "live"; }
  else if (state === "down") { connEl.classList.add("down"); connEl.textContent = "offline"; }
  else connEl.textContent = "connecting";
}

function showHandoff(ev) {
  handoffTitle.textContent = ev.green ? "Run finished — green!" : "Run finished — not green";
  const rows = [];
  if (lastTests) {
    rows.push(`tests — ${lastTests.passed} passed, ${lastTests.failed} failed, ${lastTests.skipped} skipped`);
  }
  if (lastUsage) {
    rows.push(`tokens — ${lastUsage.tokens_used.toLocaleString()} of ${lastUsage.ceiling.toLocaleString()} (${lastUsage.calls} calls)`);
  }
  if (lastIdea) rows.push(`brief — “${lastIdea}”`);
  handoffStats.replaceChildren(...rows.map((row) => {
    const li = document.createElement("li");
    li.textContent = row;
    return li;
  }));
  setConsole("finished");
}

// ── the pairing flow: Brief → run + pair code → poll until the companion claims it ──
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function startPolling(runId) {
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      const status = await (await fetch(`/runs/${runId}`)).json();
      if (status.status === "ready" && consoleState === "pairing") {
        setConsole("connected");
        log(`your machine is connected (${status.workspace}) — notes to the team are live`);
      } else if (status.status === "disconnected" && consoleState === "connected") {
        pairStatusEl.textContent = "machine disconnected — re-run the same command to resume";
        setConsole("pairing");
        log("your machine disconnected — the run is paused, not lost");
      }
    } catch {
      // keep polling; a dropped request is not a dropped run
    }
  }, 2000);
}

function showPairing(run) {
  activeRun = run;
  pairCodeEl.textContent = run.pair_code;
  pairCmdEl.textContent = run.connect;
  pairStatusEl.textContent = "waiting for your machine…";
  setConsole("pairing");
  log(`run created — pair code ${run.pair_code}`);
  startPolling(run.run_id);
}

function handle(ev) {
  switch (ev.kind) {
    case "run_started":
      scene.reset();
      // kickoff meeting: the analyst and the PM take the new idea to the table
      scene.kickoff("requirements");
      scene.kickoff("planner");
      lastTests = null;
      lastUsage = null;
      lastIdea = ev.idea;
      // a demo rehearsal replaces any real run still waiting for a machine
      stopPolling();
      activeRun = null;
      ciEl.className = "ci ci-idle";
      ciEl.textContent = "building…";
      setConsole("running");
      log(`run started — “${ev.idea}”`);
      break;
    case "run_finished":
      scene.celebrate();
      log(ev.green ? "run finished — green!" : "run finished — not green");
      showHandoff(ev);
      break;
    case "agent_status": {
      scene.setStatus(ev.agent.id, ev.status, ev.detail);
      // the floor mirrors real office behaviour for each beat of the pipeline
      const { id, status } = { id: ev.agent.id, status: ev.status };
      if (id === "requirements" && status === "working") scene.goStation("requirements", "whiteboard");
      else if (id === "requirements" && status === "done") scene.goHome("requirements");
      else if (id === "planner" && status === "thinking") scene.goHome("planner");
      else if (id === "reporter" && status === "working") scene.goStation("reporter", "printer");
      else if (id === "reporter" && status === "done") scene.goHome("reporter");
      else if (id === "fixer" && status === "done") scene.goHome("fixer");
      if (ev.detail) log(`${ev.agent.label.split(" ")[0]} ${ev.detail}`);
      break;
    }
    case "task_assigned":
      scene.setTask(ev.agent_id, ev.task.title);
      scene.pinTask(ev.task.id, ev.agent_id);
      scene.collectCard(ev.agent_id); // standup: walk to the board, take your card
      log(`task “${ev.task.title}” → ${ev.agent_id}`);
      break;
    case "message_sent":
      scene.message(ev.from_agent, ev.to_agent);
      // a cry for help sends the fixer walking to the sender's desk
      if (ev.to_agent === "fixer") scene.goHelp("fixer", ev.from_agent);
      log(`${ev.from_agent} → ${ev.to_agent}: ${ev.summary}`);
      break;
    case "test_run_finished": {
      lastTests = ev;
      scene.testRun(ev.green);
      ciEl.className = `ci ${ev.green ? "ci-green" : "ci-red"}`;
      ciEl.textContent = ev.green ? `GREEN ${ev.passed}✓` : `RED ${ev.failed}✗`;
      log(`tests — ${ev.passed} passed, ${ev.failed} failed`);
      break;
    }
    case "note_added":
      // the client envelope: flies in from off-screen, pins a lemon sticky
      scene.clientNote();
      log(`you → the team: ${ev.text}`, true);
      break;
    case "usage_tick": {
      lastUsage = ev;
      const pct = Math.min(100, (ev.tokens_used / ev.ceiling) * 100);
      meterFill.style.width = `${pct}%`;
      meterLabel.textContent = `${ev.tokens_used.toLocaleString()} / ${ev.ceiling.toLocaleString()} tokens`;
      break;
    }
  }
}

function loop(now) {
  scene.frame(now);
  requestAnimationFrame(loop);
}

// ── the Brief ──
ideaInput.addEventListener("input", () => {
  briefBtn.disabled = !ideaInput.value.trim();
});
for (const chip of document.querySelectorAll(".example-chip")) {
  chip.addEventListener("click", () => {
    ideaInput.value = chip.textContent;
    briefBtn.disabled = false;
  });
}

async function startRun(idea) {
  briefBtn.disabled = true;
  stopPolling();
  try {
    const resp = await (
      await fetch("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idea }),
      })
    ).json();
    if (resp.error || resp.detail) {
      log(`run refused: ${resp.error || resp.detail}`);
      return;
    }
    if (!resp.pair_code) {
      // the classic symptom of an old server process serving new static files
      log("the server answered but has no run registry — restart uvicorn so it serves the current build");
      return;
    }
    showPairing(resp);
  } catch (err) {
    log(`could not create the run: ${err.message}`);
  } finally {
    setTimeout(() => { briefBtn.disabled = !ideaInput.value.trim(); }, 2000);
  }
}

copyCmdBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(pairCmdEl.textContent);
    copyCmdBtn.textContent = "copied";
    setTimeout(() => (copyCmdBtn.textContent = "copy"), 1200);
  } catch {
    log("copy failed — select the command manually");
  }
});

briefBtn.addEventListener("click", () => startRun(ideaInput.value.trim()));
ideaInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && ideaInput.value.trim()) {
    e.preventDefault();
    startRun(ideaInput.value.trim());
  }
});

// the header button stays as a quick rehearsal trigger with the default idea
demoBtn.addEventListener("click", async () => {
  demoBtn.disabled = true;
  try {
    await fetch("/demo/run", { method: "POST" });
    log("demo run queued — watch the floor");
  } finally {
    setTimeout(() => (demoBtn.disabled = false), 2000);
  }
});

// ── the Note Dock ──
dockEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = noteInput.value.trim();
  if (!text) return;
  noteInput.value = "";
  // a connected real run gets the planner mailbox; the demo feed gets its echo
  const url =
    consoleState === "connected" && activeRun ? `/runs/${activeRun.run_id}/notes` : "/demo/note";
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  } catch (err) {
    log(`note failed to send: ${err.message}`);
  }
});

// ── the Handoff ──
againBtn.addEventListener("click", () => {
  stopPolling();
  activeRun = null;
  setConsole("idle");
  ideaInput.focus();
});

async function start() {
  setConn("wait");
  setConsole("idle");
  const roster = await (await fetch("/api/agents")).json();
  for (const ref of roster) scene.addAgent(ref);
  requestAnimationFrame(loop);

  const source = new EventSource("/events/stream");
  source.onopen = () => setConn("live");
  source.onerror = () => setConn("down");
  source.onmessage = (msg) => {
    try { handle(JSON.parse(msg.data)); } catch (e) { console.error("bad event", e); }
  };
}

start();
