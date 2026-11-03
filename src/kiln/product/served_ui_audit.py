"""Filesystem audit: official integrations must appear in the served UI."""

from __future__ import annotations

import json
import re
from pathlib import Path

# Surfaces we know how to check by name in the idea / discovery.
_KNOWN_PACKAGES: dict[str, tuple[str, ...]] = {
    "excalidraw": ("@excalidraw/excalidraw", "excalidraw"),
    "monaco": ("monaco-editor", "@monaco-editor"),
    "leaflet": ("leaflet",),
    "mapbox": ("mapbox-gl",),
    "codemirror": ("codemirror", "@codemirror"),
    "tiptap": ("@tiptap/core", "@tiptap/react"),
    "quill": ("quill",),
    "chart.js": ("chart.js",),
    "d3": ("d3",),
}

_OFFICIAL_HINT = re.compile(
    r"official\s+package|official\s+embed|npm\s+package|from\s+npm",
    re.IGNORECASE,
)
_STUB_HINT = re.compile(r"\b(deliberate\s+stub|labeled\s+stub|cdn\b)", re.IGNORECASE)
_UI_DIRECTION = re.compile(r"##\s*UI\s*direction\b", re.IGNORECASE)
_INTEGRATION = re.compile(r"##\s*Integration\s*method\b", re.IGNORECASE)
_NA = re.compile(r"^n/?a\b", re.IGNORECASE)

_SERVED_GLOBS = (
    "public/**/*",
    "static/**/*",
    "templates/**/*",
    "client/**/*",
    "frontend/**/*",
    "dist/**/*",
    "ui/**/*",
    "*.html",
    "src/**/*.html",
    "src/**/*.{js,jsx,ts,tsx,css}",
    "app/**/*.{html,js,jsx,ts,tsx,css}",
)

_STUB_HOST = re.compile(
    r"id=[\"']?(drawing-host|canvas-host|editor-host|whiteboard-host)[\"']?",
    re.IGNORECASE,
)
_TEXTAREA = re.compile(r"<textarea\b", re.IGNORECASE)
_PASTE_JSON = re.compile(r"paste\s+json|json\s+textarea|drawing\s+json", re.IGNORECASE)


def _section_body(discovery: str, heading: re.Pattern[str]) -> str | None:
    parts = re.split(r"\n(?=##\s)", discovery)
    for block in parts:
        first = block.lstrip().split("\n", 1)[0]
        if heading.search(first):
            return re.sub(r"^##[^\n]*\n?", "", block).strip()
    return None


def _wanted_packages(idea: str, discovery: str) -> list[tuple[str, tuple[str, ...]]]:
    blob = f"{idea}\n{discovery}".lower()
    wanted: list[tuple[str, tuple[str, ...]]] = []
    for name, markers in _KNOWN_PACKAGES.items():
        if name in blob or any(m.lower() in blob for m in markers):
            wanted.append((name, markers))
    return wanted


def _deps_blob(root: Path) -> str:
    parts: list[str] = []
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            deps = data.get(key) or {}
            if isinstance(deps, dict):
                parts.extend(deps.keys())
        parts.append(pkg.read_text(encoding="utf-8"))
    req = root / "requirements.txt"
    if req.is_file():
        parts.append(req.read_text(encoding="utf-8"))
    return "\n".join(parts).lower()


def _iter_served_files(root: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    patterns = [
        "public/**/*.*",
        "static/**/*.*",
        "templates/**/*.*",
        "client/**/*.*",
        "frontend/**/*.*",
        "dist/**/*.*",
        "ui/**/*.*",
        "*.html",
        "src/**/*.html",
        "src/**/*.js",
        "src/**/*.jsx",
        "src/**/*.ts",
        "src/**/*.tsx",
        "src/**/*.css",
        "app/**/*.html",
        "app/**/*.js",
        "app/**/*.jsx",
        "app/**/*.ts",
        "app/**/*.tsx",
        "app/**/*.css",
    ]
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            if "node_modules" in path.parts or ".git" in path.parts:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
    return found


def _served_blob(files: list[Path]) -> str:
    chunks: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(f"// {path.as_posix()}\n{text}")
    return "\n".join(chunks)


def audit_served_ui(
    root: Path,
    *,
    idea: str = "",
    discovery: str = "",
) -> list[str]:
    """Return evidence-backed product gaps; empty list means the audit cleared."""
    root = Path(root)
    gaps: list[str] = []
    discovery = discovery or ""
    if not discovery:
        disc_path = root / "docs" / "discovery.md"
        if disc_path.is_file():
            discovery = disc_path.read_text(encoding="utf-8", errors="replace")

    integration = _section_body(discovery, _INTEGRATION) or ""
    ui_direction = _section_body(discovery, _UI_DIRECTION)
    stub_integration = bool(
        _STUB_HINT.search(integration) and not _OFFICIAL_HINT.search(integration)
    )
    official = bool(_OFFICIAL_HINT.search(integration)) or (
        bool(integration.strip())
        and not _NA.match(integration.strip())
        and not stub_integration
    )

    wanted = _wanted_packages(idea, discovery)
    deps = _deps_blob(root)
    served_files = _iter_served_files(root)
    served = _served_blob(served_files).lower()

    # Enforce package + served mount when the brief wants a known surface and
    # discovery did not explicitly choose a CDN/stub.
    if wanted and not stub_integration:
        for name, markers in wanted:
            in_deps = any(m.lower() in deps for m in markers)
            in_served = any(m.lower() in served for m in markers)
            must_have = official or name in idea.lower() or any(
                m.lower() in idea.lower() for m in markers
            )
            if must_have and not in_deps:
                gaps.append(
                    f"integration ({name}): package not listed in dependencies "
                    f"(expected one of {', '.join(markers)})"
                )
            elif in_deps and not in_served:
                gaps.append(
                    f"integration ({name}): dependency present but served UI "
                    f"(HTML/JS/CSS under public/static/client/…) never loads "
                    f"{markers[0]} — mount/bundle missing"
                )

    # Stub host: drawing/editor host that is only a textarea / paste-JSON.
    for path in served_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _STUB_HOST.search(text):
            continue
        lower = text.lower()
        has_package = any(
            m.lower() in lower for markers in _KNOWN_PACKAGES.values() for m in markers
        )
        if (_TEXTAREA.search(text) or _PASTE_JSON.search(text)) and not has_package:
            gaps.append(
                f"stub host in {path.as_posix().replace(chr(92), '/')}: "
                "host element wraps a textarea/JSON fallback and never loads "
                "the official package"
            )

    if ui_direction is not None:
        body = ui_direction.strip()
        if body and not _NA.match(body):
            design = root / "docs" / "design.md"
            if not design.is_file():
                gaps.append(
                    "UI direction was set in discovery but docs/design.md is missing"
                )
            css_files = [
                p
                for p in served_files
                if p.suffix.lower() in {".css", ".scss"} or p.name.endswith(".css")
            ]
            html_style = any(
                "stylesheet" in _served_blob([p]).lower() or "<style" in _served_blob([p]).lower()
                for p in served_files
                if p.suffix.lower() in {".html", ".htm"}
            )
            if not css_files and not html_style:
                gaps.append(
                    "UI direction was set but no stylesheet (CSS) was found in "
                    "served UI paths"
                )

    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for gap in gaps:
        if gap not in seen:
            seen.add(gap)
            out.append(gap)
    return out
