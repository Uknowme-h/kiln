from pathlib import Path

from kiln.product.served_ui_audit import audit_served_ui


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_mounted_excalidraw_clears(tmp_path: Path):
    _write(
        tmp_path,
        "package.json",
        '{"dependencies":{"@excalidraw/excalidraw":"^0.17.0"}}',
    )
    _write(
        tmp_path,
        "public/app.js",
        'import { Excalidraw } from "@excalidraw/excalidraw";\n'
        'document.getElementById("drawing-host").appendChild(root);\n',
    )
    _write(tmp_path, "public/index.html", '<div id="drawing-host"></div>')
    _write(
        tmp_path,
        "docs/discovery.md",
        "## Integration method\nOfficial npm package @excalidraw/excalidraw\n\n"
        "## UI direction\nWarm stone palette.\n",
    )
    _write(tmp_path, "docs/design.md", "tokens: --bg, --ink\n")
    _write(tmp_path, "public/styles.css", ":root { --bg: #111; }\n")
    gaps = audit_served_ui(
        tmp_path,
        idea="notes with excalidraw",
        discovery=(tmp_path / "docs/discovery.md").read_text(encoding="utf-8"),
    )
    assert gaps == []


def test_stub_textarea_host_is_a_gap(tmp_path: Path):
    _write(
        tmp_path,
        "package.json",
        '{"dependencies":{"@excalidraw/excalidraw":"^0.17.0"}}',
    )
    _write(
        tmp_path,
        "public/index.html",
        '<div id="drawing-host"><textarea placeholder="paste JSON"></textarea></div>',
    )
    discovery = (
        "## Integration method\nOfficial npm package @excalidraw/excalidraw\n"
    )
    gaps = audit_served_ui(
        tmp_path, idea="excalidraw whiteboard", discovery=discovery
    )
    assert any("mount/bundle missing" in g or "stub host" in g for g in gaps)


def test_deliberate_stub_skips_package_enforcement(tmp_path: Path):
    discovery = "## Integration method\nDeliberate stub — textarea only\n"
    gaps = audit_served_ui(
        tmp_path, idea="excalidraw later", discovery=discovery
    )
    assert not any("package not listed" in g for g in gaps)


def test_ui_direction_requires_design_and_css(tmp_path: Path):
    discovery = "## UI direction\nIndustrial mono, sharp terminal.\n"
    gaps = audit_served_ui(tmp_path, idea="clean UI notes", discovery=discovery)
    assert any("design.md" in g for g in gaps)
    assert any("stylesheet" in g for g in gaps)
