/* Glue: load the roster, subscribe to the SSE event contract, drive the scene,
 * and run the client console: Brief (idle) → Pairing (waiting for the local
 * companion) → Connected (notes live against the real run). The header demo
 * button still plays the scripted rehearsal feed: running → Handoff. The
 * header auto-approve switch (and "Allow all" in the approval dialog) lets
 * commands run unattended; the plan review and questions always ask. */
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
// answered here and dismissed optimistically; their approval_resolved is
// still on its way and must not close the NEXT pending dialog
let answeredLocally = [];

// Decisions auto-approve never makes for the user (companion NEVER_AUTO_APPROVE)
const NEVER_AUTO = new Set(["plan", "question"]);

function isQuestionEvent(ev) {
  return ev && (ev.action === "question" || (ev.questions && ev.questions.length));
}

function isCommandEvent(ev) {
  return ev && !isQuestionEvent(ev) && !NEVER_AUTO.has(ev.action);
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
  approvalAll.classList.toggle("hidden", !isCommandEvent(ev));
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

// A floor-side approval_resolved: the terminal answered (close its dialog) or
// it is the echo of an answer given here (already closed).
function resolveApproval(ev) {
  const mine = answeredLocally.findIndex((q) => q.agent_id === ev.agent_id);
  if (mine >= 0) {
    answeredLocally.splice(mine, 1);
    return;
  }
  const match = approvalQueue.find((q) => q.agent_id === ev.agent_id) || approvalQueue[0];
  if (match) dismissApproval(match);
}

function resetApprovals() {
  approvalQueue = [];
  approvalShown = null;
  answeredLocally = [];
  pendingApprovals = 0;
  scene.setWaiting(null);
  approvalDialog.classList.add("hidden");
  questionDialog.classList.add("hidden");
  setApprovalBadge(false);
}

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
const drawerPickEl = document.getElementById("drawer-pick");
const drawerStatusEl = document.getElementById("drawer-status");
const drawerTaskEl = document.getElementById("drawer-task");
const drawerActivityEl = document.getElementById("drawer-activity");
const approvalDialog = document.getElementById("approval-dialog");
const approvalTitle = document.getElementById("approval-title");
const approvalAgent = document.getElementById("approval-agent");
const approvalLines = document.getElementById("approval-lines");
const approvalNote = document.getElementById("approval-note");
const approvalAllow = document.getElementById("approval-allow");
const approvalAlways = document.getElementById("approval-always");
const approvalAll = document.getElementById("approval-all");
const autoBtn = document.getElementById("auto-btn");
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
  if (!ev) return null;
  dismissApproval(ev); // optimistic; approval_resolved logs the outcome
  if (!activeRun) return null;
  answeredLocally.push(ev);
  try {
    const res = await postJson(`/runs/${activeRun.run_id}/approvals`, body);
    if (res.ok === false || res.error) throw new Error(res.error || "rejected");
    return res;
  } catch {
    // the answer never reached the companion — put the question back up
    answeredLocally = answeredLocally.filter((q) => q !== ev);
    if (!approvalQueue.includes(ev)) approvalQueue.unshift(ev);
    showNextApproval();
    log("your answer did not reach the companion — answer again, or use the terminal");
    return null;
  }
}

async function answerApproval(approved, scope) {
  // deny can carry the user's reason back to the agent as the tool error
  const feedback = approved ? "" : approvalFeedback.value.trim();
  const res = await postApprovalBody({ approved, scope: scope || "once", feedback });
  if (res && scope === "all") {
    setAutoApprove(true);
    log("auto-approve on — commands run without asking; the plan and questions still ask");
  }
}

async function answerQuestions(answers) {
  await postApprovalBody({ approved: true, answers });
}

approvalAllow.addEventListener("click", () => answerApproval(true, "once"));
approvalAlways.addEventListener("click", () => answerApproval(true, "run"));
approvalAll.addEventListener("click", () => answerApproval(true, "all"));
approvalDeny.addEventListener("click", () => answerApproval(false, "once"));

// ── auto-approve: the floor's "allow all commands" switch ──
let autoApprove = false;

function setAutoApprove(on) {
  autoApprove = !!on;
  autoBtn.classList.toggle("on", autoApprove);
  autoBtn.setAttribute("aria-pressed", String(autoApprove));
  autoBtn.textContent = `auto-approve: ${autoApprove ? "on" : "off"}`;
}

function showAutoButton(on) {
  autoBtn.classList.toggle("hidden", !on);
}

autoBtn.addEventListener("click", async () => {
  if (!activeRun) return;
  const enable = !autoApprove;
  if (enable && !window.confirm(
    "Auto-approve every command the agents run (shell, installs, network) for the rest of this run?\n\n" +
    "The plan review and the agents' questions will still ask you."
  )) return;
  // a command waiting on screen is answered by the same switch
  if (enable && isCommandEvent(approvalShown)) {
    await answerApproval(true, "all");
    return;
  }
  autoBtn.disabled = true;
  try {
    const res = await postJson(`/runs/${activeRun.run_id}/auto-approve`, { enabled: enable });
    if (res.ok === false || res.error) throw new Error(res.error || "rejected");
    setAutoApprove(res.auto_approve);
    log(enable
      ? "auto-approve on — commands run without asking; the plan and questions still ask"
      : "auto-approve off — commands ask again");
  } catch (err) {
    log(`auto-approve did not reach the companion: ${err.message}`);
  } finally {
    autoBtn.disabled = false;
  }
});

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}
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
let liveRunId = null; // run_id from the last run_started (the demo's is "demo-1")
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
  scene.setDimmed(state !== "running" && state !== "connected");
  // a real run in progress owns the floor; the rehearsal would scramble it
  demoBtn.disabled = !!activeRun && state !== "idle" && state !== "finished";
  showAutoButton(!!activeRun && ["browsing", "connected", "running"].includes(state));
}

// the real run is on the floor (not the rehearsal feed)
function realRunLive() {
  return !!activeRun && liveRunId === activeRun.run_id;
}

// `kind`: true / "you" for the user's own notes, "team" for the developers
// talking, "lead" for the tech lead's escalations and decisions
function log(text, kind) {
  const li = document.createElement("li");
  li.textContent = text;
  if (kind) li.className = kind === true ? "you" : kind;
  logEl.prepend(li);
  while (logEl.children.length > 60) logEl.lastChild.remove();
}

// ── the team panel: two developers, their tasks, the lead ──
const teamEmptyEl = document.getElementById("team-empty");
const teamLanesEl = document.getElementById("team-lanes");
const teamTasksEl = document.getElementById("team-tasks");
const teamMetaEl = document.getElementById("team-meta");
const handoffNotesWrap = document.getElementById("handoff-notes-wrap");
const handoffNotesEl = document.getElementById("handoff-notes");
const TEAM_ICONS = {
  note: "✎", question: "?", answer: "↩", checkout: "⇆", contract: "§",
  status: "•", escalation: "⚑", decision: "★",
};

function who(id) {
  const a = scene.agent(id);
  if (a) return a.label;
  if (id === "kiln") return "Kiln";
  if (id === "supervisor") return "Lena Lead";
  return id;
}

function renderTeam(ev) {
  const lanes = ev.lanes || [];
  teamEmptyEl.classList.toggle("hidden", lanes.length > 0);
  teamLanesEl.replaceChildren(...lanes.map((lane) => {
    const div = document.createElement("div");
    div.className = `team-lane${lane.current ? " busy" : ""}${lane.paused ? " paused" : ""}`;
    const name = document.createElement("div");
    const strong = document.createElement("span");
    strong.className = "who";
    strong.textContent = lane.label;
    name.append(strong);
    if (lane.focus && lane.focus !== "shared" && !lane.label.includes(lane.focus)) {
      const focus = document.createElement("span");
      focus.className = "focus";
      focus.textContent = lane.focus;
      name.append(focus);
    }
    const now = document.createElement("div");
    now.className = "now";
    now.textContent = lane.current
      ? `${lane.paused ? "paused on " : "on "}${lane.current} — ${lane.current_title || ""}`
      : "between tasks";
    div.append(name, now);
    if ((lane.checked_out || []).length) {
      const files = document.createElement("div");
      files.className = "files";
      files.textContent = `editing shared: ${lane.checked_out.join(", ")}`;
      div.append(files);
    }
    div.addEventListener("click", () => openDrawer(lane.agent_id));
    div.title = "open this developer's work";
    return div;
  }));
  teamTasksEl.replaceChildren(...(ev.tasks || []).map((task) => {
    const li = document.createElement("li");
    const st = document.createElement("span");
    st.className = `st st-${task.status}`;
    st.textContent = task.status;
    const title = document.createElement("span");
    title.textContent = `${task.id} ${task.title}`;
    const owner = document.createElement("span");
    owner.className = "owner";
    owner.textContent = (who(task.agent_id) || "").replace(/\s*\(.*\)$/, "");
    li.append(st, title, owner);
    return li;
  }));
  const meta = [];
  if (ev.open_questions) meta.push(`${ev.open_questions} open question${ev.open_questions > 1 ? "s" : ""}`);
  if (ev.contract_changes) meta.push(`${ev.contract_changes} contract change${ev.contract_changes > 1 ? "s" : ""}`);
  if (ev.decisions) meta.push(`${ev.decisions} lead decision${ev.decisions > 1 ? "s" : ""}`);
  teamMetaEl.textContent = meta.join(" · ");
}

function resetTeam() {
  teamEmptyEl.classList.remove("hidden");
  teamLanesEl.replaceChildren();
  teamTasksEl.replaceChildren();
  teamMetaEl.textContent = "";
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
  // what went wrong and how the team recovered: failed tasks, repairs,
  // contract changes, the tech lead's decisions
  const notes = ev.notes || [];
  handoffNotesWrap.classList.toggle("hidden", notes.length === 0);
  handoffNotesEl.replaceChildren(...notes.map((text) => {
    const li = document.createElement("li");
    li.textContent = text;
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
      if (!activeRun || activeRun.run_id !== runId) return; // a newer Brief took over
      if (typeof status.auto_approve === "boolean" && status.auto_approve !== autoApprove) {
        setAutoApprove(status.auto_approve);
      }
      const live = realRunLive();
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
        // a reconnect mid-build goes straight back to the live floor
        setConsole(live ? "running" : "connected");
        log(live
          ? "your machine is back — the team picks up where it left off"
          : `workspace locked (${status.workspace || "…"}) — the run is on`);
      } else if (
        status.status === "disconnected" &&
        (consoleState === "connected" || consoleState === "browsing" || consoleState === "running")
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
  setAutoApprove(false); // the poll reports the companion's real state once it pairs
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
      liveRunId = ev.run_id;
      closeDrawer();
      resetPeople();
      resetApprovals();
      resetTeam();
      // a real run keeps polling so a mid-build disconnect shows up on the
      // floor; a rehearsal has nothing to poll
      if (!realRunLive()) stopPolling();
      // keep activeRun: its transcript stays fetchable from the Handoff screen
      ciEl.className = "ci ci-idle";
      ciEl.textContent = "building…";
      setConsole("running");
      log(`run started — “${ev.idea}”`);
      break;
    case "run_finished":
      if (realRunLive()) stopPolling();
      resetApprovals();
      scene.celebrate(ev.green);
      log(ev.green ? "run finished — green!" : "run finished — not green");
      showHandoff(ev);
      break;
    case "agent_status": {
      // Dynamically seat workers (and any role) that were not in the static roster;
      // the event's AgentRef is authoritative for the desk label
      scene.ensureAgent(ev.agent, true);
      scene.setStatus(ev.agent.id, ev.status, ev.detail);
      const p = person(ev.agent);
      p.status = ev.status;
      p.detail = ev.detail || "";
      note(ev.agent.id, ev.detail ? `${ev.status} — ${ev.detail}` : ev.status);
      if (drawerAgent === ev.agent.id) renderStatus();
      // the floor mirrors real office behaviour for each beat of the pipeline
      const { id, status } = { id: ev.agent.id, status: ev.status };
      if (id === "requirements" && status === "working") scene.goStation("requirements", "whiteboard");
      else if (id === "requirements" && status === "done") scene.goHome("requirements");
      // the planner thinks on their feet, pacing by the whiteboard
      else if (id === "planner" && status === "thinking") scene.goStation("planner", "pace");
      else if (id === "planner") scene.goHome("planner");
      else if (id === "reporter" && status === "working") scene.goStation("reporter", "printer");
      else if (id === "reporter" && status === "done") scene.goHome("reporter");
      else if (id === "fixer" && status === "done") scene.goHome("fixer");
      // the lead walks back to the meeting table once a decision is made
      else if (id === "supervisor" && (status === "done" || status === "stuck")) scene.goHome("supervisor");
      if (ev.detail) {
        const name = /^Dev [A-Z]/.test(ev.agent.label) ? ev.agent.label.slice(0, 5) : ev.agent.label.split(" ")[0];
        log(`${name} ${ev.detail}`, id === "supervisor" ? "lead" : undefined);
      }
      break;
    }
    case "task_assigned": {
      const wid = ev.agent_id;
      const n = /^worker-(\d+)$/.exec(wid);
      // keep the label the developer's own status events set ("Dev A (backend)")
      const known = scene.agent(wid);
      scene.ensureAgent({
        id: wid,
        role: "worker",
        label: known ? known.label : n ? `Dev ${String.fromCharCode(64 + Number(n[1]))}` : wid,
      });
      scene.setTask(wid, ev.task.title);
      scene.pinTask(ev.task.id, wid);
      const p = person({ id: wid, role: "worker", label: (scene.agent(wid) || {}).label || wid });
      p.task = ev.task.title;
      note(wid, `took the task “${ev.task.title}”`);
      if (drawerAgent === wid) renderStatus();
      scene.collectCard(wid); // standup: walk to the board, take your card
      log(`task “${ev.task.title}” → ${wid}`);
      break;
    }
    case "message_sent":
      scene.message(ev.from_agent, ev.to_agent);
      // a cry for help sends the fixer walking to the sender's desk
      if (ev.to_agent === "fixer") scene.goHelp("fixer", ev.from_agent);
      log(`${ev.from_agent} → ${ev.to_agent}: ${ev.summary}`);
      note(ev.from_agent, `sent ${ev.to_agent}: ${ev.summary}`);
      note(ev.to_agent, `from ${ev.from_agent}: ${ev.summary}`);
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
      if (ev.auto_approved) {
        // already allowed by auto-approve: a log line, never a dialog
        const a = scene.agent(ev.agent_id);
        const what = (ev.lines && ev.lines[0]) || ev.action;
        log(`auto-approved — ${a ? a.label.split(" ")[0] : ev.agent_id}: ${what}`);
        note(ev.agent_id, `auto-approved ${ev.action}: ${what}`);
        if (!autoApprove) setAutoApprove(true);
        break;
      }
      note(ev.agent_id, isQuestionEvent(ev)
        ? "asked you some questions"
        : `waiting for your approval to ${ev.action}: ${(ev.lines && ev.lines[0]) || ev.summary || ""}`);
      scene.setWaiting(ev.agent_id, true);
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
      if (ev.auto_approved) break; // its pending line already said so
      pendingApprovals = Math.max(0, pendingApprovals - 1);
      setApprovalBadge(pendingApprovals > 0);
      resolveApproval(ev);
      scene.setWaiting(ev.agent_id, false);
      log(ev.approved ? "approval granted" : "approval refused");
      note(ev.agent_id, ev.approved ? "you approved it" : "you refused it");
      break;
    case "team_event": {
      // the developers (and the lead) talking: an envelope between desks,
      // a log line, and both people's activity
      scene.message(ev.from_agent, ev.to_agent);
      if (ev.category === "escalation" && /^worker-\d+$|^fixer$/.test(ev.from_agent)) {
        scene.goHelp("supervisor", ev.from_agent); // the lead walks over to listen
      }
      const icon = TEAM_ICONS[ev.category] || "•";
      const lead = ev.category === "escalation" || ev.category === "decision";
      log(`${icon} ${who(ev.from_agent)} → ${who(ev.to_agent)}: ${ev.text}`, lead ? "lead" : "team");
      note(ev.from_agent, `${icon} to ${who(ev.to_agent)}: ${ev.text}`);
      note(ev.to_agent, `${icon} from ${who(ev.from_agent)}: ${ev.text}`);
      break;
    }
    case "team_update":
      renderTeam(ev);
      break;
    case "transcript_tick": {
      // the one-line summary feeds the activity list; an open drawer on this
      // agent (or its sub-agents) pulls the full entries
      const root = rootId(ev.agent_id);
      const sub = ev.agent_id.slice(root.length + 1);
      if (ev.summary) note(ev.agent_id, sub ? `${sub}: ${ev.summary}` : ev.summary);
      if (drawerAgent === root && ev.seq > drawerSeq) refreshDrawer(false);
      break;
    }
  }
}

// ── the agent drawer: click someone, see what they are doing and their I/O ──
let drawerAgent = null; // agent id currently shown
let drawerSeq = 0; // highest entry seq rendered, for live append
let drawerOpener = null; // focus goes back here when the drawer closes

const ROLE_NAMES = {
  requirements: "Requirements analyst", planner: "Planner", test_author: "Test author",
  worker: "Developer", fixer: "Fixer", reviewer: "Reviewer", reporter: "Reporter",
  supervisor: "Tech lead",
};
const STATUS_COLORS = {
  idle: "#a199ab", thinking: "#4fa3af", working: "#d8a93c", stuck: "#e06a62", done: "#5fb07f",
};

// Everyone who has been on the floor this run, including workers who have
// since handed their desk over, so their work stays one click away.
const people = new Map(); // id -> { label, role, status, detail, task }
const activity = new Map(); // id -> [{ at: Date, text }], newest last
const ACTIVITY_CAP = 60;

function rootId(id) { return String(id).split("/")[0]; }

function person(ref) {
  if (!ref || !ref.id) return null;
  let p = people.get(ref.id);
  if (!p) {
    p = { label: ref.label || ref.id, role: ref.role || "", status: "idle", detail: "", task: "" };
    people.set(ref.id, p);
  }
  if (ref.label) p.label = ref.label;
  if (ref.role) p.role = ref.role;
  return p;
}

function note(id, text) {
  if (!id || !text) return;
  const key = rootId(id);
  if (!people.has(key)) return; // "kiln", "client", "orchestrator" are not desks
  let list = activity.get(key);
  if (!list) activity.set(key, (list = []));
  list.push({ at: new Date(), text });
  if (list.length > ACTIVITY_CAP) list.splice(0, list.length - ACTIVITY_CAP);
  if (drawerAgent === key) renderActivity();
}

function resetPeople() {
  activity.clear();
  for (const [id, p] of people) {
    if (!scene.agent(id)) people.delete(id); // last run's departed workers
    else Object.assign(p, { status: "idle", detail: "", task: "" });
  }
}

function closeDrawer() {
  if (!drawerAgent) return;
  drawerAgent = null;
  scene.setFocus(null);
  drawerEl.classList.add("hidden");
  drawerEl.setAttribute("aria-hidden", "true");
  if (drawerOpener && document.contains(drawerOpener)) drawerOpener.focus();
  drawerOpener = null;
}

function renderStatus() {
  const p = people.get(drawerAgent);
  if (!p) return;
  const dot = document.createElement("span");
  dot.className = "dot";
  dot.style.background = STATUS_COLORS[p.status] || STATUS_COLORS.idle;
  const state = document.createElement("span");
  state.className = "state";
  state.textContent = p.status;
  const detail = document.createElement("span");
  const here = scene.agent(drawerAgent);
  detail.textContent = p.detail || (here ? "" : "has left the floor — their work is below");
  drawerStatusEl.replaceChildren(dot, state, detail);
  drawerTaskEl.textContent = p.task || "";
  drawerTaskEl.classList.toggle("hidden", !p.task);
}

function renderActivity() {
  const list = activity.get(drawerAgent) || [];
  if (!list.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "nothing yet — this fills in as they work";
    drawerActivityEl.replaceChildren(li);
    return;
  }
  const items = list.slice().reverse().map(({ at, text }) => {
    const li = document.createElement("li");
    const time = document.createElement("time");
    time.dateTime = at.toISOString();
    time.textContent = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const span = document.createElement("span");
    span.textContent = text;
    li.append(time, span);
    return li;
  });
  drawerActivityEl.replaceChildren(...items);
}

function renderPicker(extraIds) {
  const ids = new Set(people.keys());
  for (const id of extraIds || []) if (people.has(rootId(id))) ids.add(rootId(id));
  const options = [...ids].map((id) => {
    const p = people.get(id);
    const opt = document.createElement("option");
    opt.value = id;
    const away = scene.agent(id) ? "" : " (left)";
    opt.textContent = `${p.label} — ${ROLE_NAMES[p.role] || p.role || "agent"}${away}`;
    opt.selected = id === drawerAgent;
    return opt;
  });
  drawerPickEl.replaceChildren(...options);
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
  // work this agent delegated (an explore sub-agent, a context compaction)
  const sub = entry.agent_id && entry.agent_id !== drawerAgent
    ? entry.agent_id.slice(rootId(entry.agent_id).length + 1)
    : "";
  wrap.className = `tx-entry tx-${entry.kind}${sub ? " tx-sub" : ""}`;
  const head = document.createElement("div");
  head.className = "tx-head";
  head.textContent = `#${entry.seq} ${entry.kind.replace("_", " ")}`;
  if (sub) {
    const who = document.createElement("span");
    who.className = "tx-who";
    who.textContent = ` · ${sub}`;
    head.appendChild(who);
  }
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

// Ticks arrive in bursts (one per recorded entry); at most one fetch is in
// flight, and a burst during it is folded into a single follow-up fetch.
let drawerFetching = false;
let drawerAgain = false;

async function refreshDrawer(full) {
  if (!drawerAgent) return;
  if (!activeRun) {
    if (full) {
      drawerBody.replaceChildren(block("transcript",
        "Model and tool I/O is recorded during a real run. The rehearsal only plays floor events — see Activity above.",
        "tx-result"));
    }
    return;
  }
  if (drawerFetching && !full) { drawerAgain = true; return; }
  const agentId = drawerAgent;
  drawerFetching = true;
  drawerAgain = false;
  try {
    const url = `/runs/${activeRun.run_id}/transcript?agent=${encodeURIComponent(agentId)}` +
      (full ? "" : `&after=${drawerSeq}`);
    const data = await (await fetch(url)).json();
    if (drawerAgent !== agentId) return; // the drawer moved to another desk meanwhile
    if (full) { drawerBody.replaceChildren(); drawerSeq = 0; renderPicker(data.agents); }
    if (!data.entries || (!data.entries.length && full)) {
      if (full) drawerBody.appendChild(block("transcript", "no recorded I/O for this agent yet", "tx-result"));
      return;
    }
    if (!full && data.entries.length && drawerSeq === 0) drawerBody.replaceChildren(); // drop the placeholder
    const stick = drawerBody.scrollHeight - drawerBody.scrollTop - drawerBody.clientHeight < 40;
    const frag = document.createDocumentFragment();
    for (const entry of data.entries) {
      if (entry.seq <= drawerSeq) continue;
      frag.appendChild(renderEntry(entry));
      drawerSeq = entry.seq;
    }
    drawerBody.appendChild(frag);
    // follow the live tail, but never yank someone reading further up
    if (full || stick) drawerBody.scrollTop = drawerBody.scrollHeight;
  } catch { /* a dropped fetch is not a dropped drawer */ }
  finally {
    drawerFetching = false;
    if (drawerAgain && drawerAgent) refreshDrawer(false);
  }
}

function openDrawer(agentId) {
  const a = scene.agent(agentId);
  const p = a ? person(a) : people.get(agentId);
  if (!p) return;
  if (!drawerAgent) drawerOpener = document.activeElement;
  const switching = drawerAgent !== agentId;
  drawerAgent = agentId;
  scene.setFocus(agentId);
  drawerTitle.textContent = p.label;
  const role = ROLE_NAMES[p.role] || p.role || "agent";
  drawerSub.textContent = activeRun ? `${role} · run ${activeRun.run_id}` : `${role} · rehearsal`;
  renderPicker();
  renderStatus();
  renderActivity();
  drawerEl.classList.remove("hidden");
  drawerEl.setAttribute("aria-hidden", "false");
  if (switching) drawerBody.replaceChildren();
  refreshDrawer(true);
}

function floorPoint(ev) {
  const rect = canvas.getBoundingClientRect();
  return [
    ((ev.clientX - rect.left) / rect.width) * canvas.width,
    ((ev.clientY - rect.top) / rect.height) * canvas.height,
  ];
}
canvas.addEventListener("click", (ev) => {
  const id = scene.agentAt(...floorPoint(ev));
  if (id) openDrawer(id);
});
let hoverPending = false;
canvas.addEventListener("mousemove", (ev) => {
  if (hoverPending) return; // one hit test per frame is plenty
  hoverPending = true;
  requestAnimationFrame(() => {
    hoverPending = false;
    const id = scene.agentAt(...floorPoint(ev));
    canvas.style.cursor = id ? "pointer" : "default";
    const p = id && people.get(id);
    canvas.title = p ? `${p.label} — click to see their work` : "";
  });
});
drawerPickEl.addEventListener("change", () => openDrawer(drawerPickEl.value));
drawerClose.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// ── crisp pixels: size the canvas so every art pixel lands on whole screen pixels ──
const stageWrapEl = canvas.parentElement;
const ART_PX = 3; // one sprite pixel is 3 canvas pixels
function fitCanvas() {
  const box = stageWrapEl.getBoundingClientRect();
  if (!box.width || !box.height) return;
  const dpr = window.devicePixelRatio || 1;
  const fit = Math.min(box.width / canvas.width, box.height / canvas.height);
  const steps = Math.floor(fit * ART_PX * dpr); // whole screen pixels per art pixel
  const snapped = steps / (ART_PX * dpr);
  // snap when it costs little; on a 1× screen the steps are coarse (⅓, ⅔, 1),
  // and shrinking the floor by a quarter is worse than slightly uneven pixels
  const scale = steps >= 1 && snapped >= fit * 0.88 ? snapped : fit;
  canvas.style.width = `${Math.floor(canvas.width * scale)}px`;
  canvas.style.height = `${Math.floor(canvas.height * scale)}px`;
}
new ResizeObserver(fitCanvas).observe(stageWrapEl);
// a browser zoom changes devicePixelRatio without resizing the box
(function watchDpr() {
  const mq = matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
  mq.addEventListener("change", () => { fitCanvas(); watchDpr(); }, { once: true });
})();

// ── motion and time preferences ──
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
scene.setReducedMotion(reducedMotion.matches);
reducedMotion.addEventListener("change", (e) => scene.setReducedMotion(e.matches));
// ?hour=21 previews the floor at that time of day (the lighting follows the clock)
{
  const hour = parseFloat(new URLSearchParams(location.search).get("hour"));
  if (!Number.isNaN(hour)) scene.setClock(hour);
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
  // a real run (connected, or building on the floor) gets the planner
  // mailbox; the rehearsal feed gets its echo
  const real = activeRun && (consoleState === "connected" || realRunLive());
  const url = real ? `/runs/${activeRun.run_id}/notes` : "/demo/note";
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
  liveRunId = null;
  setAutoApprove(false);
  setConsole("idle");
  ideaInput.focus();
});

async function start() {
  setConn("wait");
  setConsole("idle");
  try {
    const roster = await (await fetch("/api/agents")).json();
    for (const ref of roster) { scene.addAgent(ref); person(ref); }
  } catch (err) {
    // the floor still runs; agents are seated as their events arrive
    log(`could not load the roster: ${err.message}`);
  }
  requestAnimationFrame(loop);

  const source = new EventSource("/events/stream");
  source.onopen = () => setConn("live");
  source.onerror = () => setConn("down");
  source.onmessage = (msg) => {
    try { handle(JSON.parse(msg.data)); } catch (e) { console.error("bad event", e); }
  };
}

start();
