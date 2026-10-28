/* Glue: load the roster, subscribe to the SSE event contract, drive the scene,
 * and run the client console: Brief (idle) → Note Dock (running) → Handoff
 * (finished). The console talks to the demo endpoints for now; the same
 * handlers switch to POST /runs and /runs/{id}/notes when real runs exist. */
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
const handoffEl = document.getElementById("handoff");
const handoffTitle = document.getElementById("handoff-title");
const handoffStats = document.getElementById("handoff-stats");
const againBtn = document.getElementById("again-btn");

// ── console state machine ──
let consoleState = "idle"; // idle | running | finished
let lastTests = null; // last test_run_finished event
let lastUsage = null; // last usage_tick event
let lastIdea = "";

function setConsole(state) {
  consoleState = state;
  briefEl.classList.toggle("hidden", state !== "idle");
  handoffEl.classList.toggle("hidden", state !== "finished");
  dockEl.classList.toggle("hidden", state !== "running");
  stageWrap.classList.toggle("dimmed", state !== "running");
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
  try {
    // demo feed today; POST /runs {idea} replaces this when real runs exist
    await fetch("/demo/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea }),
    });
    log("brief delivered — watch the floor");
  } finally {
    setTimeout(() => { briefBtn.disabled = !ideaInput.value.trim(); }, 2000);
  }
}

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
  try {
    // demo echo today; POST /runs/{id}/notes replaces this when real runs exist
    await fetch("/demo/note", {
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
