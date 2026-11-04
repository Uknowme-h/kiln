"""Zero-token reproduction of the 1006 drop: a 70s planner model call.

Real server, real detached companion, scripted model. Requirements answers
instantly; the planner's task-graph call sleeps 70s — the length of the calls
that dropped run-a609a72c. Before the fix the server's event loop froze for
that call and the companion's keepalive closed the socket (~40s in). After the
fix the companion must stay attached and the planner must finish.

    .venv\\Scripts\\python scripts/ws_slow_model_check.py
"""
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PORT = 8098
WORKSPACE = Path("D:/tmp/kiln-slow-model-check")
SLOW_SECONDS = 70

SPEC = {
    "title": "Tasks", "summary": "A small task list.", "stack": "Python",
    "stack_id": "python", "frameworks": [], "roots": [],
    "user_stories": [{"id": "s1", "as_a": "user", "i_want": "to add a task", "so_that": "I remember it"}],
    "acceptance_criteria": [{
        "id": "c1", "story_id": "s1", "given": "a list", "when": "I add one",
        "then": "it is stored", "oracle": "tests/test_tasks.py — adding a task persists it",
    }],
    "file_plan": [{"path": "app.py", "purpose": "the app"}],
    "assumptions": [],
}
GRAPH = {
    "spec_title": "Tasks",
    "tasks": [{"id": "t1", "title": "API", "description": "REST endpoints for tasks",
               "owned_paths": ["app.py"], "depends_on": [], "kind": "feature",
               "root": "", "required_skills": [], "covers": ["c1"], "assignee": None}],
    "contract": None,
}


def _scripted_complete(self, *, messages, response_format=None, tools=None, **_):
    from kiln.gateway.client import Completion

    name = ((response_format or {}).get("json_schema") or {}).get("name", "")
    if name == "spec":
        content = json.dumps(SPEC)
    elif name == "task_graph":
        time.sleep(SLOW_SECONDS)  # the long, blocking model call
        content = json.dumps(GRAPH)
    else:
        content = "done exploring"  # tool turns end with no tool calls
    return Completion(content=content, model="scripted", prompt_tokens=10, completion_tokens=10)


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def main() -> int:
    import uvicorn
    from kiln.config import Settings
    from kiln.gateway import client as gateway_client
    from kiln.main import create_app

    gateway_client.OpenAITransport.complete = _scripted_complete
    if "--simulate-old" in sys.argv:
        # reproduce the pre-fix planner: its model calls run ON the event loop
        import types

        from kiln.agents import planner

        async def _blocking_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        planner.asyncio = types.SimpleNamespace(to_thread=_blocking_to_thread)
        print("simulating the OLD planner (model call blocks the event loop)")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    for stale in WORKSPACE.glob("*"):
        if stale.is_file():
            stale.unlink()

    settings = Settings(
        _env_file=None, model_api_key="scripted", sandbox="host", debug=True,
        ask_requirements=False, enable_project_skills=False,
    )
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(40):
        try:
            _get("/health")
            break
        except OSError:
            time.sleep(0.25)

    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/runs", data=json.dumps({"idea": "a task list API"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    created = json.loads(urllib.request.urlopen(req, timeout=10).read())
    run_id = created["run_id"]
    log_path = WORKSPACE / ".companion.log"
    flags = 0x00000200 | 0x00000008 | 0x01000000  # NEW_PROCESS_GROUP | DETACHED | BREAKAWAY
    companion = subprocess.Popen(
        [sys.executable, "-m", "kiln.companion.cli", "connect", created["pair_code"],
         "--dir", str(WORKSPACE), "--server", f"http://127.0.0.1:{PORT}", "--auto-approve"],
        stdout=open(log_path, "w"), stderr=subprocess.STDOUT, cwd=ROOT,
        creationflags=flags, close_fds=True,
    )
    print(f"run {run_id}; planner task-graph call will block for {SLOW_SECONDS}s")

    start = time.time()
    planned = False
    while time.time() - start < SLOW_SECONDS + 60:
        time.sleep(5)
        log = log_path.read_text(encoding="utf-8", errors="replace")
        if "dropped the connection" in log or companion.poll() is not None:
            tail = log[-400:].encode("ascii", "replace").decode("ascii")
            print(f"FAIL at {int(time.time() - start)}s - companion dropped:\n{tail}")
            companion.kill()
            return 1
        events = (ROOT / ".kiln" / "runs" / run_id / "events.jsonl")
        text = events.read_text(encoding="utf-8") if events.is_file() else ""
        if "tasks planned" in text:
            planned = True
            break
        print(f"  [{int(time.time() - start):>3}s] companion attached, waiting on the slow planner call")

    status = _get(f"/runs/{run_id}")["status"]
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(companion.pid)], capture_output=True,
                   creationflags=0x08000000)
    server.should_exit = True
    if planned:
        print(f"\nPASS — companion stayed attached through a {SLOW_SECONDS}s model call; "
              f"planner finished (run status: {status})")
        return 0
    print(f"\nFAIL — planner never finished (status {status})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
