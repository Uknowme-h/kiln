from kiln.companion.commandscan import scan_command


def test_compound_commands_split_and_classify():
    scan = scan_command("pytest -q && npm install && curl https://x.dev", "posix")
    assert not scan.opaque
    assert len(scan.segments) == 3
    assert scan.segments[0].kind == "run" and scan.segments[0].low_risk
    assert scan.segments[1].kind == "install"
    assert scan.segments[2].kind == "network"
    assert scan.installs and scan.network
    assert not scan.rememberable


def test_destructive_commands_are_flagged():
    scan = scan_command("rm -rf /", "posix")
    assert scan.destructive
    scan = scan_command("Format C:", "cmd")
    assert scan.destructive


def test_opaque_commands_cannot_be_remembered():
    scan = scan_command("echo $(whoami)", "posix")
    assert scan.opaque
    assert not scan.rememberable
    scan = scan_command("iex (curl x)", "powershell")
    assert scan.opaque


def test_version_probes_and_pytest_are_low_risk():
    scan = scan_command("python --version", "posix")
    assert scan.rememberable
    scan = scan_command("python -m pytest -q", "posix")
    assert scan.rememberable
    scan = scan_command("npm install left-pad", "posix")
    assert not scan.rememberable
