"""Static SPA-mount audit: is the served app shell actually reachable?

A green API suite proves nothing about the served UI when the SPA entry never
mounts the app shell. run-9f836de2 shipped exactly that: ``frontend/src/main.jsx``
rendered only ``<Landing/>`` while ``frontend/src/App.jsx`` (full router) and five
page components sat unused — dead code no test imported.

This audit is static and deterministic — no browser, no model:

1. Find HTML entries with a ``<script type="module" src="...">`` (Vite/CRA SPA).
2. Resolve each entry module and walk its transitive import closure (static,
   dynamic ``import()``, and ``require()``), across every HTML entry found.
3. Flag mount points the closure never reaches: an ``App.{js,jsx,ts,tsx}`` shell,
   and page-level components under ``pages/`` / ``views/`` / ``routes/``.

It only fires when an SPA entry is found, and only flags page-level mount
points (never utility components), so a landing-only app or a non-SPA stack
stays silent.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["audit_spa_mount"]

# Where an SPA index commonly lives, relative to the workspace root.
_ENTRY_HTML_GLOBS = (
    "index.html",
    "frontend/index.html",
    "client/index.html",
    "public/index.html",
    "web/index.html",
    "app/index.html",
)

_SCRIPT_SRC = re.compile(
    r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE
)

# import ... from 'x' | import 'x' | import('x') | require('x') | export ... from 'x'
_IMPORT = re.compile(
    r"""
    \bimport\s+(?:[^'";]+?\s+from\s+)?['"]([^'"]+)['"]
    | \bimport\s*\(\s*['"]([^'"]+)['"]\s*\)
    | \brequire\s*\(\s*['"]([^'"]+)['"]\s*\)
    | \bexport\s+(?:[^'";]+?\s+from\s+)['"]([^'"]+)['"]
    """,
    re.VERBOSE,
)

_RESOLVE_SUFFIXES = ("", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_RESOLVE_INDEX = ("index.js", "index.jsx", "index.ts", "index.tsx")

# Page-level mount points we flag when unreachable. Utility components elsewhere
# are not the audit's business.
_PAGE_DIR = re.compile(r"(^|/)(pages|views|routes)/", re.IGNORECASE)
_APP_SHELL = re.compile(r"(^|/)App\.(js|jsx|ts|tsx)$")

# Never treat these as mount points.
_EXCLUDE = re.compile(
    r"(\.test\.|\.spec\.|\.stories\.|\.d\.ts$|__tests__|__mocks__|node_modules|/dist/|/build/)",
    re.IGNORECASE,
)

_CODE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

# Directories that are never the app's own served source. Skipped during the
# walk entirely — not just filtered from results — so the audit neither
# false-positives on a dependency's pages/ nor pays the node_modules/.venv walk.
_SKIP_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "env",
    ".git",
    "__pycache__",
    "dist",
    "build",
    ".pytest_cache",
    ".ruff_cache",
    ".kiln",
}

# Router libraries whose presence in the closure means pages are plausibly wired.
_ROUTER_HINT = re.compile(
    r"react-router|@tanstack/react-router|wouter|@reach/router|vue-router|createRouter",
    re.IGNORECASE,
)


def _is_code(path: Path) -> bool:
    return path.suffix.lower() in _CODE_SUFFIXES


def _workspace_rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _find_entries(root: Path) -> list[Path]:
    entries: list[Path] = []
    for rel in _ENTRY_HTML_GLOBS:
        candidate = root / rel
        if candidate.is_file():
            entries.append(candidate)
    return entries


def _script_entries(html_path: Path) -> list[str]:
    try:
        text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    srcs = []
    for tag in re.finditer(r"<script\b[^>]*>", text, re.IGNORECASE):
        block = tag.group(0)
        if "module" not in block:
            continue
        match = _SCRIPT_SRC.search(block)
        if match:
            srcs.append(match.group(1))
    return srcs


def _resolve_module(
    base_dir: Path,
    spec: str,
    *,
    served_root: Path,
    alias_root: Path | None,
) -> Path | None:
    """Resolve one import specifier to a file on disk, or None when external.

    `served_root` is the directory absolute "/..." paths resolve against (the
    HTML entry's dir for Vite). `alias_root` is the `src/` dir "@/..." maps to.
    """
    spec = spec.strip()
    if spec.startswith("@/") and alias_root is not None:
        candidate = alias_root / spec[2:]
    elif spec.startswith("./") or spec.startswith("../"):
        candidate = (base_dir / spec).resolve()
    elif spec.startswith("/"):
        # Vite-style absolute from the served root (the HTML entry's directory).
        candidate = served_root / spec.lstrip("/")
    else:
        return None  # bare package import — not workspace code
    for suffix in _RESOLVE_SUFFIXES:
        trial = Path(str(candidate) + suffix)
        if trial.is_file():
            return trial
    if candidate.is_dir():
        for index in _RESOLVE_INDEX:
            trial = candidate / index
            if trial.is_file():
                return trial
    return None


def _reachable_closure(root: Path) -> tuple[set[str], list[str]]:
    """Union of every HTML entry's transitive import closure (workspace-relative).

    Returns (reachable, entry_modules). Both empty when no SPA entry exists.
    """
    reachable: set[str] = set()
    entry_modules: list[str] = []
    visited: set[Path] = set()
    queue: list[Path] = []

    for html in _find_entries(root):
        served_root = html.parent
        for src in _script_entries(html):
            resolved = _resolve_module(
                html.parent, src, served_root=served_root, alias_root=None
            )
            if resolved is not None:
                queue.append(resolved)
                entry_modules.append(_workspace_rel(root, resolved))

    while queue:
        current = queue.pop(0)
        current = current.resolve()
        if current in visited or not _is_code(current):
            continue
        visited.add(current)
        reachable.add(_workspace_rel(root, current))
        # The served root for absolute "/..." imports: the ancestor that holds
        # this module's top-level source tree (e.g. frontend/ for
        # frontend/src/main.jsx). We approximate it as the parent of the
        # outermost `src/` ancestor, falling back to the workspace root.
        alias_root: Path | None = None
        served_root = root
        src_ancestors = [p for p in current.parents if p.name == "src"]
        if src_ancestors:
            alias_root = src_ancestors[-1]
            served_root = alias_root.parent
        try:
            text = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _IMPORT.finditer(text):
            spec = next(group for group in match.groups() if group)
            target = _resolve_module(
                current.parent, spec, served_root=served_root, alias_root=alias_root
            )
            if target is not None and target.resolve() not in visited:
                queue.append(target)

    return reachable, entry_modules


def _page_mount_points(root: Path) -> list[str]:
    """Every page-level mount point in the workspace (App shell + pages/views/routes)."""
    points: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.name not in _SKIP_DIRS:
                    stack.append(child)
                continue
            if not child.is_file() or not _is_code(child):
                continue
            rel = _workspace_rel(root, child)
            if _EXCLUDE.search(rel):
                continue
            if _APP_SHELL.search(rel) or _PAGE_DIR.search(rel):
                points.append(rel)
    return sorted(points)


def audit_spa_mount(root: Path) -> list[str]:
    """Evidence-backed gaps for an SPA shell the entry never mounts.

    Empty list means either the mount is wired, or this is not an SPA we can
    statically resolve (the audit stays silent rather than guess).
    """
    root = Path(root)
    reachable, entry_modules = _reachable_closure(root)
    if not entry_modules:
        return []  # no SPA entry found — nothing deterministic to say

    gaps: list[str] = []
    entry_label = ", ".join(entry_modules)

    mount_points = _page_mount_points(root)
    unreachable = [rel for rel in mount_points if rel not in reachable]
    if not unreachable:
        return []

    app_shells = [rel for rel in unreachable if _APP_SHELL.search(rel)]
    pages = [rel for rel in unreachable if not _APP_SHELL.search(rel)]

    has_router = any(_ROUTER_HINT.search(rel) for rel in reachable)

    for shell in app_shells:
        gaps.append(
            f"spa-mount: {shell} exists but is never imported from the entry "
            f"{entry_label} — the app shell is dead code, so the routed/management "
            "UI it defines cannot be served"
        )
    if pages:
        listed = ", ".join(pages[:6]) + (" …" if len(pages) > 6 else "")
        router_note = (
            "no router is reachable from the entry either"
            if not has_router
            else "the reachable router never references them"
        )
        gaps.append(
            f"spa-mount: {len(pages)} page component(s) are unreachable from the "
            f"entry {entry_label} ({listed}) — {router_note}; these pages cannot "
            "be served"
        )
    return gaps
