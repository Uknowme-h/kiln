import asyncio
import json

from kiln.companion import client
from kiln.companion.cli import _looks_like_kiln_repo
from kiln.companion.executor import Executor


def test_a_plain_workspace_is_fine(tmp_path):
    assert _looks_like_kiln_repo(tmp_path) is False


def test_the_kiln_repo_is_refused(tmp_path):
    (tmp_path / "src" / "kiln").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "kiln"\n', encoding="utf-8")
    assert _looks_like_kiln_repo(tmp_path) is True


def test_a_src_kiln_dir_alone_is_not_enough(tmp_path):
    (tmp_path / "src" / "kiln").mkdir(parents=True)
    assert _looks_like_kiln_repo(tmp_path) is False


class _FakeConnect:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *_args):
        return False


class _ClosingSocket:
    """Acks the hello, then drops the link like a server restart."""

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        return json.dumps({"type": "hello_ack", "run_id": "run-1"})

    def __aiter__(self):
        async def gen():
            from websockets.exceptions import ConnectionClosedError
            from websockets.frames import Close

            raise ConnectionClosedError(Close(1012, "service restart"), None)
            yield

        return gen()


def test_a_server_restart_is_a_clean_message_not_a_traceback(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        "websockets.connect", lambda *a, **k: _FakeConnect(_ClosingSocket())
    )
    executor = Executor(tmp_path)
    code = asyncio.run(client.serve("ws://x/companion/ws", "ABC123", str(tmp_path), executor))
    assert code == 3
    out = capsys.readouterr().out
    assert "dropped the connection" in out
    assert "1012" in out
    assert "Brief the team again" in out
