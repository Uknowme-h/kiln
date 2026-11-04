from pathlib import Path

from kiln.stacks.profiles import install_command, parse_version, profile_for


def test_parse_version_reads_the_first_dotted_number():
    assert parse_version("Python 3.12.4") == (3, 12, 4)
    assert parse_version("v20.11.0") == (20, 11, 0)
    assert parse_version("nope") is None


def test_profiles_expose_detect_install_test_and_smoke():
    py = profile_for("python")
    node = profile_for("node")
    assert py.needs[0].min_version >= (3, 10)
    assert node.needs[0].min_version >= (20, 0)
    workspace = Path(".")
    assert any("pytest" in " ".join(cmd) for cmd in [py.test_command(workspace, "Linux")])
    assert "vitest" in " ".join(node.test_command(workspace, "Linux"))


def test_install_command_picks_a_manager_on_path():
    tools = {"winget": r"C:\winget.exe", "brew": None}
    assert install_command("python", "Windows", tools)[0] == "winget"
    tools = {"brew": "/opt/homebrew/bin/brew"}
    assert install_command("node", "macOS", tools)[0] == "brew"
    assert install_command("python", "Windows", {}) is None


def test_tsc_smoke_waits_until_node_modules_exists(tmp_path):
    # before deps are installed, tsc would fail on missing modules — that is a
    # missing install, not a broken file, so the smoke check skips it
    node = profile_for("node")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    commands = node.smoke_commands(tmp_path, ["app.ts"], "Linux")
    assert not any("tsc" in " ".join(cmd) for cmd in commands)
    (tmp_path / "node_modules").mkdir()
    commands = node.smoke_commands(tmp_path, ["app.ts"], "Linux")
    assert any("tsc" in " ".join(cmd) for cmd in commands)


def test_smoke_commands_for_root_compiles_a_secondary_root(tmp_path):
    # a python secondary root (backend/) gets a whole-tree compile check
    py = profile_for("python")
    backend = tmp_path / "backend" / "app"
    backend.mkdir(parents=True)
    (backend / "main.py").write_text("x = 1\n", encoding="utf-8")
    commands = py.smoke_commands_for_root(tmp_path, "backend", "Linux")
    assert commands and "compileall" in " ".join(commands[0])
    # a node secondary root only type-checks once deps + tsconfig are present
    node = profile_for("node")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    assert node.smoke_commands_for_root(tmp_path, "frontend", "Linux") == []
    (frontend / "node_modules").mkdir()
    (frontend / "tsconfig.json").write_text("{}", encoding="utf-8")
    commands = node.smoke_commands_for_root(tmp_path, "frontend", "Linux")
    assert any("tsc" in " ".join(cmd) for cmd in commands)
