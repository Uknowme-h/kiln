"""Live monitored eval: a real end-to-end Kiln run, instrumented.

Boots uvicorn (server) on a scratch port, spawns the real companion with
``--auto-approve`` (shell/install approvals auto-run; ask_user and the plan
beat would still ask — this prompt is chosen to avoid them), drives one golden
idea to completion, then reports:

- per-role and per-agent prompt/completion tokens (vs the run-9f836de2 baseline)
- whether the scaffold was deterministic (no hand-written Vite/package.json in
  the transcript as the primary path) and official fingerprints landed
- the mechanical product checklist (evals/checklist.py)

Usage (from the repo root, .env holds MODEL_API_KEY):

    .venv\\Scripts\\python scripts/eval_live_run.py
    .venv\\Scripts\\python scripts/eval_live_run.py --idea-file evals/prompts/yes-app.md \
        --workspace D:/tmp/yes-app-eval --timeout 2400

The run is real (real Meta model, real scaffold CLIs, real tests). It can take
tens of minutes and a few hundred thousand tokens. Ctrl-C stops it cleanly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Baseline for comparison: the run-9f836de2 disaster this work sets out to fix.
BASELINE = {
    "run": "run-9f836de2",
    "total_prompt_tokens": 3_720_751,
    "worker_prompt_tokens": 3_270_272,
    "calls": 145,
}


def _post(port: int, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as resp:
        return json.loads(resp.read())


def _wait_for_server(port: int, deadline: float = 20) -> None:
    start = time.time()
    while time.time() - start < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return
        except OSError:
            time.sleep(0.3)
    raise SystemExit("server did not come up")


# ── analysis ─────────────────────────────────────────────────────────────────

CONFIG_MARKERS = ("vite.config", "package.json", "create vite", "create-vite", "npm create")


def _transcript_config_writes(workspace: Path) -> list[dict]:
    """Tool calls in the transcript where an AGENT hand-wrote a build config.

    The whole point of the deterministic scaffold is that these should not
    exist: the companion writes vite.config/package.json via the recipe, and no
    worker should be authoring them by hand. We flag write_file/edit_file calls
    that name a config path.
    """
    hits: list[dict] = []
    for line in (workspace / "transcript.jsonl").read_text(encoding="utf-8", errors="replace").splitlines() if (workspace / "transcript.jsonl").is_file() else []:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("kind") != "tool_call":
            continue
        data = entry.get("data") or {}
        if data.get("name") not in ("write_file", "edit_file"):
            continue
        path = str((data.get("args") or {}).get("path", ""))
        if any(marker in path for marker in ("vite.config", "package.json", "tsconfig")):
            hits.append({"agent": entry.get("agent_id"), "path": path})
    return hits


def _fingerprints(workspace: Path) -> dict:
    return {
        "frontend/vite.config.ts": (workspace / "frontend" / "vite.config.ts").is_file()
        or (workspace / "frontend" / "vite.config.js").is_file(),
        "frontend/package.json": (workspace / "frontend" / "package.json").is_file(),
        "backend/app/main.py": (workspace / "backend" / "app" / "main.py").is_file(),
        "single-root package.json": (workspace / "package.json").is_file(),
        "tests present": (workspace / "tests").is_dir(),
    }


def _usage_report(workspace: Path) -> dict:
    summary = workspace / "summary.json"
    if summary.is_file():
        try:
            return json.loads(summary.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _print_report(
    *,
    run_id: str,
    green: bool | None,
    wall: float,
    usage: dict,
    fingerprints: dict,
    config_writes: list[dict],
    checklist: dict,
) -> None:
    print("\n" + "=" * 72)
    print(f"EVAL REPORT — {run_id}")
    print("=" * 72)
    print(f"green: {green}   wall: {wall:.0f}s")
    u = (usage or {}).get("usage") or {}
    total_prompt = u.get("prompt_tokens", 0)
    total_calls = u.get("calls", 0)
    by_role = u.get("by_role") or {}
    worker_prompt = (by_role.get("worker") or {}).get("prompt_tokens", 0)
    print(f"\nTokens: {total_prompt:,} prompt / {u.get('completion_tokens', 0):,} completion / {total_calls} calls")
    print(f"  baseline {BASELINE['run']}: {BASELINE['total_prompt_tokens']:,} prompt, {BASELINE['calls']} calls")
    if total_prompt:
        delta = (BASELINE["total_prompt_tokens"] - total_prompt) / BASELINE["total_prompt_tokens"] * 100
        print(f"  vs baseline: {delta:+.0f}% prompt tokens")
    print(f"  worker prompt: {worker_prompt:,}  (baseline {BASELINE['worker_prompt_tokens']:,})")
    print("\nPer-role prompt tokens:")
    for role, stats in sorted(by_role.items(), key=lambda kv: -(kv[1].get("prompt_tokens", 0))):
        print(f"  {role:<14} {stats.get('prompt_tokens', 0):>12,}  ({stats.get('calls', 0)} calls)")
    print("\nDeterministic scaffold / fingerprints:")
    for name, ok in fingerprints.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    print(f"\nAgent hand-written build configs in transcript (want 0): {len(config_writes)}")
    for hit in config_writes[:20]:
        print(f"  !! {hit['agent']} wrote {hit['path']}")
    if checklist:
        print(f"\nProduct checklist: {'PASS' if checklist.get('pass') else 'FAIL'}")
        for row in checklist.get("rows", []):
            mark = "x" if row["pass"] else ("~" if row.get("optional") else " ")
            print(f"  [{mark}] {row['id']}: {row['detail'][0] if row['detail'] else ''}")
    print("=" * 72)


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idea-file", default=str(ROOT / "evals" / "prompts" / "yes-app.md"))
    parser.add_argument("--idea", default="", help="idea text (overrides --idea-file)")
    parser.add_argument("--workspace", default="", help="build folder (default: a temp dir)")
    parser.add_argument("--port", type=int, default=8042)
    parser.add_argument("--timeout", type=int, default=2400, help="max seconds to wait for the run")
    parser.add_argument("--out", default="", help="write the JSON report here")
    args = parser.parse_args()

    idea = args.idea.strip()
    if not idea:
        raw = Path(args.idea_file).read_text(encoding="utf-8")
        # golden prompt files keep baseline prose above the real idea; the idea
        # is the trailing block of plain lines (no heading/bold/code/bullet).
        block: list[str] = []
        for line in raw.splitlines():
            text = line.strip()
            if not text or text.startswith(("#", "**", "`", "-")):
                block = []  # a metadata line resets the run of plain text
                continue
            block.append(text)
        idea = " ".join(block).strip()
        if not idea:
            raise SystemExit(f"could not find an idea in {args.idea_file}")

    workspace = Path(args.workspace) if args.workspace else Path(
        __import__("tempfile").mkdtemp(prefix="kiln-eval-")
    )
    workspace.mkdir(parents=True, exist_ok=True)
    print(f"idea: {idea}")
    print(f"workspace: {workspace}")

    import uvicorn
    from kiln.config import load_settings
    from kiln.main import create_app

    settings = load_settings()
    if not settings.model_api_key:
        raise SystemExit("MODEL_API_KEY is not set in .env — cannot run a live eval")
    settings = settings.model_copy(update={"debug": True, "sandbox": "host"})

    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_server(args.port)
    print(f"server up on :{args.port}")

    created = _post(args.port, "/runs", {"idea": idea})
    run_id = created["run_id"]
    print(f"run: {run_id}  pair code: {created['pair_code']}")

    companion_log_path = workspace / ".companion.log"
    companion_log = open(companion_log_path, "w", encoding="utf-8")

    # ── detached spawn (the Bug-1 fix) ───────────────────────────────────────
    # On Windows the companion must survive its parent dying. Three kill
    # vectors, three tiers of defence (see the verdict on run-d918940d):
    #   1. console control events (CTRL_CLOSE) — beaten by DETACHED_PROCESS
    #      (no console at all) + CREATE_NEW_PROCESS_GROUP (CTRL+C/BREAK).
    #   2. an IDE Job Object with KILL_ON_JOB_CLOSE (Cursor/VS Code) — beaten
    #      only by CREATE_BREAKAWAY_FROM_JOB, and only if the job allows it;
    #      otherwise CreateProcess fails with Access denied.
    #   3. a job that forbids breakaway — beaten by asking WMI to host the
    #      process (it lands outside the caller's job entirely).
    from kiln.companion.process import kill_tree

    _CREATE_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    _DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    _BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB (not in the stdlib)

    class _DetachedChild:
        """A companion process that outlives its parent. Uniform poll/kill API
        whether it was spawned via Popen (tiers 1–2) or WMI (tier 3)."""

        def __init__(self, argv: list[str], log_path: Path):
            self._argv = argv
            self._log_path = log_path
            self._popen: subprocess.Popen | None = None
            self._wmi_pid: int | None = None
            self.mode = ""
            self._spawn(argv, log_path)

        def _spawn(self, argv: list[str], log_path: Path) -> None:
            flags = _CREATE_NEW_GROUP | _DETACHED | _BREAKAWAY
            log = open(log_path, "a", encoding="utf-8")
            try:
                self._popen = subprocess.Popen(
                    argv, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
                    creationflags=flags, close_fds=True,
                )
                self.mode = "detached+breakaway"
                return
            except OSError as exc:
                # Access denied => the hosting job forbids breakaway. Degrade to
                # console-detached (still beats CTRL_CLOSE, not the job kill).
                if getattr(exc, "winerror", None) != 5:
                    raise
            try:
                self._popen = subprocess.Popen(
                    argv, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
                    creationflags=_CREATE_NEW_GROUP | _DETACHED, close_fds=True,
                )
                self.mode = "detached (job forbids breakaway)"
                return
            except OSError:
                pass
            # Tier 3: WMI hosts the process outside the caller's job object.
            cmdline = subprocess.list2cmdline(argv)
            ps = (
                "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                f"-Arguments @{{CommandLine={json.dumps(cmdline + ' >> ' + json.dumps(str(log_path)) + ' 2>&1')}}}; "
                "$r.ProcessId"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=60, cwd=ROOT,
            )
            pid = int((out.stdout or "0").strip().splitlines()[-1])
            if pid <= 0:
                raise RuntimeError(f"WMI spawn failed: {out.stdout} {out.stderr}")
            self._wmi_pid = pid
            self.mode = f"wmi (pid {pid})"

        def poll(self) -> int | None:
            if self._popen is not None:
                return self._popen.poll()
            alive = subprocess.run(
                ["tasklist", "/FI", f"PID eq {self._wmi_pid}", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
            return None if str(self._wmi_pid) in alive.stdout else 0

        def terminate(self) -> None:
            if self._popen is not None:
                kill_tree(self._popen)  # /F /T — grandchildren (npm/vite) too
            elif self._wmi_pid:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self._wmi_pid)],
                    capture_output=True, timeout=15,
                )

    def _spawn_companion() -> _DetachedChild:
        return _DetachedChild(
            [
                sys.executable, "-m", "kiln.companion.cli", "connect", created["pair_code"],
                "--dir", str(workspace), "--server", f"http://127.0.0.1:{args.port}",
                "--auto-approve",
            ],
            companion_log_path,
        )

    companion = _spawn_companion()
    print(f"companion connected with --auto-approve ({companion.mode}); run started\n")

    def _answer_pending() -> None:
        """Auto-answer the beats --auto-approve deliberately skips.

        --auto-approve covers only shell/install commands. The plan-approval
        beat and any ask_user questionnaire still ask — for a hands-free eval
        we answer them here via the floor approvals endpoint. (For a *manual*
        eval you'd omit this and answer in the UI/terminal yourself.)
        """
        try:
            _post(args.port, f"/runs/{run_id}/approvals", {"approved": True, "scope": "run"})
        except Exception:
            pass  # nothing was waiting; the floor races the terminal anyway

    start = time.time()
    green: bool | None = None
    state = "building"
    respawns = 0
    max_respawns = 4
    try:
        while time.time() - start < args.timeout:
            time.sleep(5)
            _answer_pending()  # plan beat + ask_user; harmless when nothing waits
            # companion watchdog: the run dies the moment it detaches. If the
            # process is gone, try to respawn it (the server keeps a dropped
            # run 'disconnected' and re-claims the same code) before giving up.
            if companion.poll() is not None and state not in ("green", "failed"):
                print(f"\n!! companion exited (mode {companion.mode}) mid-run; log tail:", flush=True)
                try:
                    tail = (workspace / ".companion.log").read_text(encoding="utf-8", errors="replace")[-1500:]
                    print("---- companion log tail ----\n" + tail + "\n----------------------------", flush=True)
                except Exception:
                    pass
                if respawns < max_respawns:
                    respawns += 1
                    backoff = 2 ** respawns  # 2, 4, 8, 16s — never hammer a busy server
                    print(f"respawning companion ({respawns}/{max_respawns}) after {backoff}s…", flush=True)
                    time.sleep(backoff)
                    companion = _spawn_companion()
                    continue
                print("companion stayed down; giving up.", flush=True)
                break
            try:
                status = _get(args.port, f"/runs/{run_id}")
                state = status.get("status", state)
            except Exception as exc:  # a slow poll must not kill a 30-min run
                print(f"  (poll error, retrying: {type(exc).__name__})", flush=True)
                continue
            elapsed = int(time.time() - start)
            print(f"  [{elapsed:>4}s] status={state}", flush=True)
            if state in ("green", "failed"):
                green = state == "green"
                break
        else:
            print(f"\ntimeout after {args.timeout}s — run still {state}")
    except KeyboardInterrupt:
        print("\ninterrupted; collecting partial results")
    finally:
        wall = time.time() - start
        try:
            companion.terminate()  # kill_tree / taskkill /T — grandchildren too
        except Exception:
            pass
        companion_log.close()
        server.should_exit = True
        time.sleep(3)  # let the debug log finish writing summary.json

    # collect evidence from the run's debug dir
    debug_dir = ROOT / settings.debug_log_dir / run_id
    usage = _usage_report(debug_dir)
    fingerprints = _fingerprints(workspace)
    config_writes = _transcript_config_writes(debug_dir)

    checklist: dict = {}
    try:
        sys.path.insert(0, str(ROOT))
        from evals.checklist import run_checklist

        checklist = run_checklist(workspace, idea=idea)
    except Exception as exc:  # noqa: BLE001
        print(f"(checklist failed: {exc})")

    report = {
        "run_id": run_id,
        "idea": idea,
        "workspace": str(workspace),
        "green": green,
        "wall_clock_seconds": round(wall, 1),
        "usage": usage.get("usage", {}),
        "baseline": BASELINE,
        "fingerprints": fingerprints,
        "agent_config_writes": config_writes,
        "checklist": checklist,
    }
    _print_report(
        run_id=run_id,
        green=green,
        wall=wall,
        usage=usage,
        fingerprints=fingerprints,
        config_writes=config_writes,
        checklist=checklist,
    )
    out = Path(args.out) if args.out else ROOT / "evals" / "results" / f"{run_id}-eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport written to {out}")
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
