/* Glue: load the roster, subscribe to the SSE event contract, drive the scene,
 * and run the client console: Brief (idle) → Pairing (waiting for the local
 * companion) → Connected (notes live against the real run). The header demo
 * button still plays the scripted rehearsal feed: running → Handoff. */
"use strict";

const canvas = document.getElementById("floor");
const scene = KilnScene.create(canvas);
const logEl = document.getElementById("log");
const connEl = document.getElementById("conn");
const approvalEl = document.getElementById("approval");
const meterFill = document.getElementById("meter-fill");
const meterLabel = document.getElementById("meter-label");
const ciEl = document.getElementById("ci");
const demoBtn = document.getElementById("demo-btn");
let pendingApprovals = 0;

function setApprovalBadge(on, detail) {
  if (!approvalEl) return;
  if (on) {
    approvalEl.classList.remove("hidden");
    approvalEl.classList.add("waiting");
    approvalEl.textContent = detail || "waiting for approval";
  } else {
    approvalEl.classList.add("hidden");
    approvalEl.classList.remove("waiting");
  }
}

// ── the approval / ask_user dialogs: same prompts as the companion terminal;
// whichever the user answers first wins ──
let approvalQueue = []; // pending approval events (the companion serializes them, so ≤1 in practice)
let approvalShown = null; // the event currently on screen

function isQuestionEvent(ev) {
  return ev && (ev.action === "question" || (ev.questions && ev.questions.length));
}

function showNextApproval() {
  if (approvalShown || !approvalQueue.length) return;
  const ev = approvalQueue[0];
  approvalShown = ev;
  const a = scene.agent(ev.agent_id);
  if (isQuestionEvent(ev)) {
    showQuestionDialog(ev, a);
    return;
  }
  questionDialog.classList.add("hidden");
  approvalTitle.textContent = `Approval needed — ${ev.action}`;
  approvalAgent.textContent = `${a ? a.label : ev.agent_id} wants to ${ev.action}`;
  approvalLines.textContent = (ev.lines && ev.lines.length ? ev.lines : [ev.summary || ev.action]).join("\n");
  approvalNote.textContent = ev.note ? `note: ${ev.note}` : "";
  approvalNote.classList.toggle("hidden", !ev.note);
  approvalAlways.classList.toggle("hidden", !ev.allow_run_scope);
  approvalFeedback.value = "";
  approvalDialog.classList.remove("hidden");
}

function showQuestionDialog(ev, a) {
  approvalDialog.classList.add("hidden");
  questionTitle.textContent = "Questions for you";
  questionAgent.textContent = `${a ? a.label : ev.agent_id} needs a few answers`;
  questionList.replaceChildren();
  const qs = ev.questions && ev.questions.length ? ev.questions : [];
  qs.forEach((q, qi) => {
    const item = document.createElement("div");
    item.className = "question-item";
    item.dataset.index = String(qi);
    item.dataset.multiple = q.multiple ? "1" : "0";
    if (q.header) {
      const h = document.createElement("div");
      h.className = "question-header";
      h.textContent = q.header;
      item.appendChild(h);
    }
    const text = document.createElement("p");
    text.className = "question-text";
    text.textContent = q.question || "";
    item.appendChild(text);
    const chips = document.createElement("div");
    chips.className = "question-chips";
    (q.options || []).forEach((opt) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "question-chip";
      chip.textContent = opt;
      chip.addEventListener("click", () => {
        if (item.dataset.multiple === "1") {
          chip.classList.toggle("selected");
        } else {
          chips.querySelectorAll(".question-chip").forEach((c) => c.classList.remove("selected"));
          chip.classList.add("selected");
        }
      });
      chips.appendChild(chip);
    });
    item.appendChild(chips);
    const free = document.createElement("input");
    free.type = "text";
    free.className = "question-free";
    free.placeholder = (q.options && q.options.length) ? "or type your own…" : "type your answer…";
    free.autocomplete = "off";
    item.appendChild(free);
    questionList.appendChild(item);
  });
  if (!qs.length) {
    const p = document.createElement("p");
    p.className = "question-text";
    p.textContent = ev.summary || "Answer in the companion terminal.";
    questionList.appendChild(p);
  }
  questionDialog.classList.remove("hidden");
}

function collectQuestionAnswers() {
  return Array.from(questionList.querySelectorAll(".question-item")).map((item) => {
    const free = item.querySelector(".question-free");
    const typed = free && free.value.trim();
    if (typed) return [typed];
    const selected = Array.from(item.querySelectorAll(".question-chip.selected")).map((c) => c.textContent);
    return selected;
  });
}

function dismissApproval(ev) {
  approvalQueue = approvalQueue.filter((q) => q !== ev);
  if (approvalShown === ev) {
    approvalShown = null;
    approvalDialog.classList.add("hidden");
    questionDialog.classList.add("hidden");
    showNextApproval();
  }
}

function resetApprovals() {
  approvalQueue = [];
  approvalShown = null;
  pendingApprovals = 0;
  approvalDialog.classList.add("hidden");
  questionDialog.classList.add("hidden");
  setApprovalBadge(false);
}

const stageWrap = document.querySelector(".stage-wrap");
const briefEl = document.getElementById("brief");
const ideaInput = document.getElementById("idea-input");
const briefBtn = document.getElementById("brief-btn");
const dockEl = document.getElementById("note-dock");
const noteInput = document.getElementById("note-input");
const pairEl = document.getElementById("pairing");
const pairAuthEl = document.getElementById("pair-auth");
const pairBrowseEl = document.getElementById("pair-browse");
const pairCodeEl = document.getElementById("pair-code");
const pairCmdEl = document.getElementById("pair-cmd");
const pairStatusEl = document.getElementById("pair-status");
const copyCmdBtn = document.getElementById("copy-cmd");
const browsePathEl = document.getElementById("browse-path");
const browseListEl = document.getElementById("browse-list");
const browseRootsEl = document.getElementById("browse-roots");
const browseUpBtn = document.getElementById("browse-up");
const browseUseBtn = document.getElementById("browse-use");
const browseCreateBtn = document.getElementById("browse-create");
const browseStatusEl = document.getElementById("browse-status");
const handoffEl = document.getElementById("handoff");
const handoffTitle = document.getElementById("handoff-title");
const handoffStats = document.getElementById("handoff-stats");
const againBtn = document.getElementById("again-btn");
const drawerEl = document.getElementById("agent-drawer");
const drawerTitle = document.getElementById("drawer-title");
const drawerSub = document.getElementById("drawer-sub");
const drawerBody = document.getElementById("drawer-body");
const drawerClose = document.getElementById("drawer-close");
const approvalDialog = document.getElementById("approval-dialog");
const approvalTitle = document.getElementById("approval-title");
const approvalAgent = document.getElementById("approval-agent");
const approvalLines = document.getElementById("approval-lines");
const approvalNote = document.getElementById("approval-note");
const approvalAllow = document.getElementById("approval-allow");
const approvalAlways = document.getElementById("approval-always");
const approvalDeny = document.getElementById("approval-deny");
const approvalFeedback = document.getElementById("approval-feedback");
const questionDialog = document.getElementById("question-dialog");
const questionTitle = document.getElementById("question-title");
const questionAgent = document.getElementById("question-agent");
const questionList = document.getElementById("question-list");
const questionSubmit = document.getElementById("question-submit");
const questionSkip = document.getElementById("question-skip");

async function postApprovalBody(body) {
  const ev = approvalShown;
  if (!ev) return;
  dismissApproval(ev); // optimistic; approval_resolved logs the outcome
  if (!activeRun) return;
  try {
    const res = await (
      await fetch(`/runs/${activeRun.run_id}/approvals`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
    ).json();
    if (res.ok === false) throw new Error(res.error || "rejected");
  } catch {
    // the answer never reached the companion — put the question back up
    if (!approvalQueue.includes(ev)) approvalQueue.unshift(ev);
    showNextApproval();
    log("your answer did not reach the companion — answer again, or use the terminal");
  }
}

async function answerApproval(approved, scope) {
  // deny can carry the user's reason back to the agent as the tool error
  const feedback = approved ? "" : approvalFeedback.value.trim();
  await postApprovalBody({ approved, scope: scope || "once", feedback });
}

async function answerQuestions(answers) {
  await postApprovalBody({ approved: true, answers });
}

approvalAllow.addEventListener("click", () => answerApproval(true, "once"));
approvalAlways.addEventListener("click", () => answerApproval(true, "run"));
approvalDeny.addEventListener("click", () => answerApproval(false, "once"));
questionSubmit.addEventListener("click", () => answerQuestions(collectQuestionAnswers()));
questionSkip.addEventListener("click", () => {
  const n = questionList.querySelectorAll(".question-item").length;
  answerQuestions(Array.from({ length: n }, () => []));
});

// ── console state machine ──
let consoleState = "idle"; // idle | pairing | browsing | connected | running | finished
let lastTests = null; // last test_run_finished event
let lastUsage = null; // last usage_tick event
let lastIdea = "";
let activeRun = null; // the real run from POST /runs; kept after finish so the drawer can read its transcript
let lastWorkspace = ""; // the folder the companion builds in, from the poll
let pollTimer = null;
let browseCwd = "";
let browseParent = "";
let workspaceSubmitting = false;

function setConsole(state) {
  consoleState = state;
  briefEl.classList.toggle("hidden", state !== "idle");
  pairEl.classList.toggle("hidden", state !== "pairing" && state !== "browsing");
  if (pairAuthEl) pairAuthEl.classList.toggle("hidden", state === "browsing");
  if (pairBrowseEl) pairBrowseEl.classList.toggle("hidden", state !== "browsing");
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
    rows.push(
      `tests — ${lastTests.passed} passed, ${lastTests.failed} failed, ${lastTests.errors} errors, ${lastTests.skipped} skipped`
    );
  }
  if (lastUsage) {
    rows.push(`tokens — ${lastUsage.tokens_used.toLocaleString()} of ${lastUsage.ceiling.toLocaleString()} (${lastUsage.calls} calls)`);
  }
  if (lastIdea) rows.push(`brief — “${lastIdea}”`);
  if (lastWorkspace) rows.push(`files — ${lastWorkspace}`);
  handoffStats.replaceChildren(...rows.map((row) => {
    const li = document.createElement("li");
    li.textContent = row;
    return li;
  }));
  setConsole("finished");
}

// ── the pairing flow: Brief → listen + pair code → paste in companion → pick folder ──
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

function startPolling(runId) {
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      const status = await (await fetch(`/runs/${runId}`)).json();
      if (status.status === "paired" && (consoleState === "pairing" || consoleState === "browsing")) {
        if (consoleState !== "browsing") {
          setConsole("browsing");
          pairStatusEl.textContent = "machine paired — pick a folder";
          log("your machine paired — choose a workspace folder");
          loadBrowse("");
        }
      } else if (
        (status.status === "ready" || status.status === "building") &&
        (consoleState === "pairing" || consoleState === "browsing")
      ) {
        lastWorkspace = status.workspace || "";
        setConsole("connected");
        log(`workspace locked (${status.workspace || "…"}) — the run is on`);
      } else if (
        status.status === "disconnected" &&
        (consoleState === "connected" || consoleState === "browsing")
      ) {
        pairStatusEl.textContent = "machine disconnected — re-run listen and paste the same code";
        setConsole("pairing");
        log("your machine disconnected — the run is paused, not lost");
      } else if (status.status === "green" || status.status === "failed") {
        stopPolling(); // the Handoff arrives with the run_finished floor event
      }
    } catch {
      // keep polling; a dropped request is not a dropped run
    }
  }, 2000);
}

function showPairing(run) {
  activeRun = run;
  pairCodeEl.textContent = run.pair_code;
  pairCmdEl.textContent = run.connect || "kiln-companion listen";
  pairStatusEl.textContent = "waiting for your machine…";
  if (browseStatusEl) browseStatusEl.textContent = "";
  setConsole("pairing");
  log(`run created — pair code ${run.pair_code}`);
  startPolling(run.run_id);
}

async function loadBrowse(path) {
  if (!activeRun) return;
  if (browseStatusEl) browseStatusEl.textContent = "loading…";
  try {
    const q = path ? `?path=${encodeURIComponent(path)}` : "";
    const data = await (await fetch(`/runs/${activeRun.run_id}/browse${q}`)).json();
    if (!data.ok) {
      if (browseStatusEl) browseStatusEl.textContent = data.error || "browse failed";
      return;
    }
    browseCwd = data.cwd || "";
    browseParent = data.parent || "";
    browsePathEl.textContent = browseCwd;
    browseUpBtn.disabled = !browseParent;
    browseRootsEl.replaceChildren(
      ...(data.roots || []).map((root) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "browse-root";
        btn.textContent = root.name;
        btn.title = root.path;
        btn.addEventListener("click", () => loadBrowse(root.path));
        return btn;
      })
    );
    browseListEl.replaceChildren(
      ...(data.entries || []).map((entry) => {
        const li = document.createElement("li");
        li.textContent = entry.name;
        li.title = entry.path;
        li.addEventListener("click", () => loadBrowse(entry.path));
        return li;
      })
    );
    if (browseStatusEl) {
      browseStatusEl.textContent = (data.entries || []).length
        ? "click a folder to open it, or use this one"
        : "empty folder — you can still use it";
    }
  } catch (err) {
    if (browseStatusEl) browseStatusEl.textContent = err.message || "browse failed";
  }
}

async function confirmWorkspace(createName) {
  if (!activeRun || !browseCwd || workspaceSubmitting) return;
  let path = browseCwd;
  let create = false;
  if (createName) {
    const name = createName.trim();
    if (!name) return;
    const sep = browseCwd.includes("\\") ? "\\" : "/";
    path = browseCwd.replace(/[\\/]+$/, "") + sep + name;
    create = true;
  }
  workspaceSubmitting = true;
  browseUseBtn.disabled = true;
  browseCreateBtn.disabled = true;
  if (browseStatusEl) browseStatusEl.textContent = "locking workspace…";
  try {
    const res = await (
      await fetch(`/runs/${activeRun.run_id}/workspace`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, create }),
      })
    ).json();
    if (!res.ok) {
      if (browseStatusEl) browseStatusEl.textContent = res.error || "could not lock folder";
      return;
    }
    lastWorkspace = res.workspace || path;
    if (browseStatusEl) browseStatusEl.textContent = `locked — ${lastWorkspace}`;
    // poll will flip to connected / building
  } catch (err) {
    if (browseStatusEl) browseStatusEl.textContent = err.message || "could not lock folder";
  } finally {
    workspaceSubmitting = false;
    browseUseBtn.disabled = false;
    browseCreateBtn.disabled = false;
  }
}

browseUpBtn.addEventListener("click", () => {
  if (browseParent) loadBrowse(browseParent);
});
browseUseBtn.addEventListener("click", () => confirmWorkspace());
browseCreateBtn.addEventListener("click", () => {
  const name = window.prompt("New folder name inside the current directory:");
  if (name) confirmWorkspace(name);
});

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
      closeDrawer();
      resetApprovals();
      stopPolling();
      // keep activeRun: its transcript stays fetchable from the Handoff screen
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
      // Dynamically seat workers (and any role) that were not in the static roster
      scene.ensureAgent(ev.agent);
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
    case "task_assigned": {
      const wid = ev.agent_id;
      const n = /^worker-(\d+)$/.exec(wid);
      scene.ensureAgent({
        id: wid,
        role: "worker",
        label: n ? `Worker ${n[1]}` : wid,
      });
      scene.setTask(wid, ev.task.title);
      scene.pinTask(ev.task.id, wid);
      scene.collectCard(wid); // standup: walk to the board, take your card
      log(`task “${ev.task.title}” → ${wid}`);
      break;
    }
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
      ciEl.textContent = ev.green
        ? `GREEN ${ev.passed}✓`
        : `RED ${ev.failed}✗ ${ev.errors}⚠`;
      log(`tests — ${ev.passed} passed, ${ev.failed} failed, ${ev.errors} errors`);
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
    case "approval_pending":
      pendingApprovals += 1;
      setApprovalBadge(true, ev.summary || `${ev.agent_id} needs approval`);
      if (activeRun) {
        approvalQueue.push(ev);
        showNextApproval();
        log(
          isQuestionEvent(ev)
            ? `questions — ${ev.summary || "answer here or in the companion terminal"}`
            : `approval — ${ev.summary || ev.action} (answer here or in the companion terminal)`
        );
      } else {
        log(`approval — ${ev.summary || ev.action} (answer in the companion terminal)`);
      }
      break;
    case "approval_resolved":
      pendingApprovals = Math.max(0, pendingApprovals - 1);
      setApprovalBadge(pendingApprovals > 0);
      if (approvalQueue.length) dismissApproval(approvalQueue[0]); // answered in the terminal
      log(ev.approved ? "approval granted" : "approval refused");
      break;
    case "transcript_tick":
      // an open drawer on this agent pulls the new entries; the floor stays quiet
      if (drawerAgent === ev.agent_id && ev.seq > drawerSeq) refreshDrawer(false);
      break;
  }
}

// ── the agent drawer: click a desk, read that agent's recorded I/O ──
let drawerAgent = null; // agent id currently shown
let drawerSeq = 0; // highest entry seq rendered, for live append

function closeDrawer() {
  drawerAgent = null;
  drawerEl.classList.add("hidden");
  drawerEl.setAttribute("aria-hidden", "true");
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function block(label, text, cls) {
  const div = document.createElement("div");
  div.className = `tx-block ${cls || ""}`;
  const lab = document.createElement("div");
  lab.className = "tx-label";
  lab.textContent = label;
  const pre = document.createElement("pre");
  pre.textContent = text;
  div.append(lab, pre);
  return div;
}

function renderEntry(entry) {
  const wrap = document.createElement("div");
  wrap.className = `tx-entry tx-${entry.kind}`;
  const head = document.createElement("div");
  head.className = "tx-head";
  head.textContent = `#${entry.seq} ${entry.kind.replace("_", " ")}`;
  wrap.appendChild(head);
  const p = entry.payload || {};
  if (entry.kind === "model_call") {
    for (const m of p.messages || []) {
      const text = typeof m.content === "string" ? m.content : JSON.stringify(m.content, null, 2);
      wrap.appendChild(block(m.role, text, `tx-msg-${m.role}`));
      for (const tc of m.tool_calls || []) {
        const args = tc.function ? tc.function.arguments : tc.arguments;
        wrap.appendChild(block(`↳ calls ${tc.function ? tc.function.name : tc.name}`, pretty(args), "tx-call"));
      }
    }
    if (p.content) wrap.appendChild(block("assistant", p.content, "tx-msg-assistant"));
    for (const c of p.tool_calls || []) wrap.appendChild(block(`↳ calls ${c.name}`, pretty(c.arguments), "tx-call"));
    const meta = `${p.prompt_tokens}↑ ${p.completion_tokens}↓ tokens · ${p.finish_reason}${p.truncated ? " · TRUNCATED" : ""}`;
    const foot = document.createElement("div");
    foot.className = "tx-meta" + (p.truncated ? " tx-truncated" : "");
    foot.textContent = meta;
    wrap.appendChild(foot);
  } else if (entry.kind === "tool_call") {
    wrap.appendChild(block(p.name || "tool", pretty(p.args), "tx-call"));
  } else if (entry.kind === "tool_result") {
    if (p.error) wrap.appendChild(block(`${p.name} — error`, p.error, "tx-error"));
    else wrap.appendChild(block(p.name || "result", p.output || "(no output)", "tx-result"));
  } else {
    wrap.appendChild(block("note", p.text || JSON.stringify(p), "tx-result"));
  }
  return wrap;
}

function pretty(v) {
  if (typeof v !== "string") return JSON.stringify(v, null, 2);
  try { return JSON.stringify(JSON.parse(v), null, 2); } catch { return v; }
}

async function refreshDrawer(full) {
  if (!drawerAgent || !activeRun) return;
  try {
    const url = `/runs/${activeRun.run_id}/transcript?agent=${encodeURIComponent(drawerAgent)}` +
      (full ? "" : `&after=${drawerSeq}`);
    const data = await (await fetch(url)).json();
    if (full) { drawerBody.replaceChildren(); drawerSeq = 0; }
    if (!data.entries || (!data.entries.length && full)) {
      if (full) drawerBody.appendChild(block("transcript", "no recorded I/O for this agent yet", "tx-result"));
      return;
    }
    for (const entry of data.entries) {
      drawerBody.appendChild(renderEntry(entry));
      drawerSeq = Math.max(drawerSeq, entry.seq);
    }
    drawerBody.scrollTop = drawerBody.scrollHeight;
  } catch { /* a dropped fetch is not a dropped drawer */ }
}

function openDrawer(agentId) {
  const a = scene.agent(agentId);
  if (!a) return;
  drawerAgent = agentId;
  drawerTitle.textContent = a.label;
  drawerSub.textContent = activeRun
    ? `${a.role} · run ${activeRun.run_id}`
    : `${a.role} · no live run — transcripts exist only during a real run`;
  drawerEl.classList.remove("hidden");
  drawerEl.setAttribute("aria-hidden", "false");
  refreshDrawer(true);
}

canvas.addEventListener("click", (ev) => {
  const rect = canvas.getBoundingClientRect();
  const x = ((ev.clientX - rect.left) / rect.width) * canvas.width;
  const y = ((ev.clientY - rect.top) / rect.height) * canvas.height;
  const id = scene.agentAt(x, y);
  if (id) openDrawer(id);
});
canvas.addEventListener("mousemove", (ev) => {
  const rect = canvas.getBoundingClientRect();
  const x = ((ev.clientX - rect.left) / rect.width) * canvas.width;
  const y = ((ev.clientY - rect.top) / rect.height) * canvas.height;
  canvas.style.cursor = scene.agentAt(x, y) ? "pointer" : "default";
});
drawerClose.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

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
