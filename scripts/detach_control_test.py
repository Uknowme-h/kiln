"""Bug-1 control test: prove the hardened spawn survives its parent dying.

Boots a server, spawns the companion detached from an INTERMEDIATE parent
that exits NORMALLY right after spawning (the production case: the eval
driver's parent shell exits when a turn ends). Then it probes the companion
through the server. If the companion still answers after the parent is gone,
the detach works and the 1006 drop is dead.

Key details, learned the hard way:
- Parent death is simulated by the parent EXITING, not by taskkill /T —
  /T walks the parent-child tree and would kill the companion regardless of
  CREATE_BREAKAWAY_FROM_JOB (breakaway escapes the Job Object, not the tree).
- Auxiliary console tools (tasklist/taskkill) get CREATE_NO_WINDOW so they do
  not flash a console window when spawned from a detached parent.

    .venv\\Scripts\\python scripts/detach_control_test.py
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

PORT = 8099
WORKSPACE = Path("D:/tmp/kiln-detach-test")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW — no console flash for aux tools


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    import uvicorn
    from kiln.config import Settings
    from kiln.main import create_app

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    settings = Settings(_env_file=None, model_api_key="x", pipeline_autostart=False, sandbox="host")
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=PORT, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            break
        except OSError:
            time.sleep(0.25)

    created = _post("/runs", {"idea": "detach test"})
    run_id = created["run_id"]
    print(f"run: {run_id}  pair code: {created['pair_code']}")

    # The intermediate parent spawns the companion detached and EXITS at once.
    parent_script = WORKSPACE / "_parent.py"
    parent_script.write_text(
        "import subprocess, sys\n"
        "argv = [sys.executable, '-m', 'kiln.companion.cli', 'connect', sys.argv[1],\n"
        "        '--dir', r'" + str(WORKSPACE) + "', '--server', 'http://127.0.0.1:" + str(PORT) + "',\n"
        "        '--auto-approve']\n"
        "log = open(r'" + str(WORKSPACE / '.companion.log') + "', 'w')\n"
        "flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | 0x00000008 | 0x01000000\n"
        "try:\n"
        "    subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)\n"
        "    print('spawned detached+breakaway', flush=True)\n"
        "except OSError as e:\n"
        "    subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,\n"
        "        creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | 0x00000008, close_fds=True)\n"
        "    print('spawned detached (breakaway denied)', flush=True)\n",
        encoding="utf-8",
    )
    parent = subprocess.run(
        [sys.executable, str(parent_script), created["pair_code"]],
        capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    print(f"parent: {parent.stdout.strip()} (parent has now exited)")
    time.sleep(3)  # let the companion connect

    probe1 = _post(f"/runs/{run_id}/probe", {"tool": "list_dir", "input": {"path": "."}})
    print(f"probe with parent gone: ok={probe1.get('ok')}")

    # survive a second beat to be sure it is not a momentary reprieve
    time.sleep(10)
    probe2 = _post(f"/runs/{run_id}/probe", {"tool": "list_dir", "input": {"path": "."}})
    print(f"probe 10s later:          ok={probe2.get('ok')}")

    # cleanup: kill the companion tree (WMI or Popen) by PID from the log
    for proc in _companion_pids():
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc)],
            capture_output=True, creationflags=NO_WINDOW,
        )
    server.should_exit = True
    if probe1.get("ok") and probe2.get("ok"):
        print("\nPASS — the companion survived its parent exiting (detach works)")
        return 0
    print("\nFAIL — the companion died with its parent (job object forbids escape?)")
    return 1


def _companion_pids() -> list[int]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object {$_.CommandLine -match 'kiln.companion.cli'}).ProcessId"],
        capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW,
    )
    pids = []
    for token in (out.stdout or "").split():
        if token.isdigit():
            pids.append(int(token))
    return pids


if __name__ == "__main__":
    raise SystemExit(main())
