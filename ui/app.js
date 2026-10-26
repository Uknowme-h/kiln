/* Glue: load the roster, subscribe to the SSE event contract, drive the scene. */
"use strict";

const canvas = document.getElementById("floor");
const scene = KilnScene.create(canvas);
const logEl = document.getElementById("log");
const connEl = document.getElementById("conn");
const meterFill = document.getElementById("meter-fill");
const meterLabel = document.getElementById("meter-label");
const ciEl = document.getElementById("ci");
const demoBtn = document.getElementById("demo-btn");

function log(text) {
  const li = document.createElement("li");
  li.textContent = text;
  logEl.prepend(li);
  while (logEl.children.length > 60) logEl.lastChild.remove();
}

function setConn(state) {
  connEl.className = "chip";
  if (state === "live") { connEl.classList.add("live"); connEl.textContent = "live"; }
  else if (state === "down") { connEl.classList.add("down"); connEl.textContent = "offline"; }
  else connEl.textContent = "connecting";
}

function handle(ev) {
  switch (ev.kind) {
    case "run_started":
      scene.reset();
      // kickoff meeting: the analyst and the PM take the new idea to the table
      scene.kickoff("requirements");
      scene.kickoff("planner");
      ciEl.className = "ci ci-idle";
      ciEl.textContent = "building…";
      log(`run started — “${ev.idea}”`);
      break;
    case "run_finished":
      scene.celebrate();
      log(ev.green ? "run finished — green!" : "run finished — not green");
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
      scene.testRun(ev.green);
      ciEl.className = `ci ${ev.green ? "ci-green" : "ci-red"}`;
      ciEl.textContent = ev.green ? `GREEN ${ev.passed}✓` : `RED ${ev.failed}✗`;
      log(`tests — ${ev.passed} passed, ${ev.failed} failed`);
      break;
    }
    case "usage_tick": {
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

demoBtn.addEventListener("click", async () => {
  demoBtn.disabled = true;
  try {
    await fetch("/demo/run", { method: "POST" });
    log("demo run queued — watch the floor");
  } finally {
    setTimeout(() => (demoBtn.disabled = false), 2000);
  }
});

async function start() {
  setConn("wait");
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
