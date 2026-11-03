from kiln.companion.process import scrubbed_env
from kiln.companion.shellenv import select_shell, shell_argv


def test_windows_prefers_pwsh_then_powershell_then_cmd():
    def which_pwsh(name):
        return {"pwsh": r"C:\pwsh.exe"}.get(name)

    shell = select_shell("Windows", which=which_pwsh, environ={})
    assert shell.name == "pwsh"
    assert shell.kind == "powershell"
    argv = shell_argv(shell, "echo hi")
    assert argv[0].endswith("pwsh.exe")
    assert "-Command" in argv

    def which_powershell(name):
        return {
            "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        }.get(name)

    shell = select_shell("Windows", which=which_powershell, environ={})
    assert shell.name == "powershell"

    def which_none(_name):
        return None

    shell = select_shell(
        "Windows",
        which=which_none,
        environ={"COMSPEC": r"C:\Windows\System32\cmd.exe"},
    )
    assert shell.kind == "cmd"
    assert shell_argv(shell, "dir")[:3] == [shell.path, "/d", "/s"]


def test_macos_and_linux_pick_posix_shells():
    shell = select_shell("Darwin", which=lambda n: None, environ={})
    assert shell.name == "zsh"
    assert shell_argv(shell, "ls") == [shell.path, "-c", "ls"]

    def which_bash(name):
        return "/bin/bash" if name == "bash" else None

    shell = select_shell("Linux", which=which_bash, environ={})
    assert shell.name == "bash"


def test_scrubbed_env_drops_secret_looking_names():
    env = scrubbed_env(
        {"PATH": "/bin", "MODEL_API_KEY": "secret", "HOME": "/home/u", "TOKEN_X": "x"}
    )
    assert "MODEL_API_KEY" not in env
    assert "TOKEN_X" not in env
    assert env["AGENT"] == "1"
    assert env["PATH"] == "/bin"
