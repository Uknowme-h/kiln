"""Static SPA-mount audit: dead app shells and unreachable pages are flagged."""

from __future__ import annotations

from pathlib import Path

from kiln.product.spa_mount_audit import audit_spa_mount


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _component(name: str) -> str:
    return f"export default function {name}() {{ return null }}\n"


def _spa_shell(root: Path, *, broken: bool) -> None:
    """A Vite SPA. `broken` reproduces run-9f836de2: the entry mounts only
    Landing; App.jsx (router) and the pages are dead code."""
    _write(
        root / "frontend/index.html",
        '<!doctype html><html><body><div id="root"></div>'
        '<script type="module" src="/src/main.jsx"></script></body></html>',
    )
    if broken:
        _write(
            root / "frontend/src/main.jsx",
            'import React from "react"\nimport ReactDOM from "react-dom/client"\n'
            'import Landing from "./pages/Landing.jsx"\n'
            'ReactDOM.createRoot(document.getElementById("root")).render(<Landing />)\n',
        )
        # App.jsx exists with a router, but the entry never imports it — dead shell
        _write(
            root / "frontend/src/App.jsx",
            'import { BrowserRouter } from "react-router-dom"\n'
            "export default function App() { return null }\n",
        )
    else:
        _write(
            root / "frontend/src/main.jsx",
            'import React from "react"\nimport ReactDOM from "react-dom/client"\n'
            'import App from "./App.jsx"\nimport "./index.css"\n'
            'ReactDOM.createRoot(document.getElementById("root")).render(<App />)\n',
        )
        _write(
            root / "frontend/src/App.jsx",
            'import { BrowserRouter, Route, Routes } from "react-router-dom"\n'
            'import Auth from "./pages/Auth.jsx"\nimport Profile from "./pages/Profile.jsx"\n'
            'import Dashboard from "./pages/Dashboard.jsx"\n'
            'import Landing from "./pages/Landing.jsx"\n'
            "export default function App() { return <Routes>"
            '<Route path="/auth" element={<Auth/>}/>'
            '<Route path="/profile" element={<Profile/>}/>'
            '<Route path="/dashboard" element={<Dashboard/>}/>'
            '<Route path="/" element={<Landing/>}/></Routes> }\n',
        )
        _write(root / "frontend/src/pages/Auth.jsx", _component("Auth"))
    _write(root / "frontend/src/pages/Landing.jsx", _component("Landing"))
    _write(root / "frontend/src/pages/Profile.jsx", _component("Profile"))
    _write(root / "frontend/src/pages/Dashboard.jsx", _component("Dashboard"))


def test_a_dead_app_shell_and_unreachable_pages_are_flagged(tmp_path: Path):
    _spa_shell(tmp_path, broken=True)
    gaps = audit_spa_mount(tmp_path)
    assert gaps, "expected the unwired shell to be flagged"
    blob = "\n".join(gaps)
    assert "frontend/src/App.jsx" in blob
    assert "never imported from the entry" in blob
    assert "frontend/src/main.jsx" in blob


def test_a_wired_spa_mount_is_silent(tmp_path: Path):
    _spa_shell(tmp_path, broken=False)
    assert audit_spa_mount(tmp_path) == []


def test_no_spa_entry_stays_silent(tmp_path: Path):
    # a plain python backend with no SPA HTML entry: nothing deterministic to say
    _write(tmp_path / "backend/app.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    assert audit_spa_mount(tmp_path) == []


def test_landing_only_spa_with_no_pages_is_silent(tmp_path: Path):
    # a legitimately single-page app: entry mounts Landing, no App shell, no pages dir
    _write(
        tmp_path / "frontend/index.html",
        '<script type="module" src="/src/main.jsx"></script>',
    )
    _write(
        tmp_path / "frontend/src/main.jsx",
        'import Landing from "./Landing.jsx"\n'
        'ReactDOM.createRoot(document.getElementById("root")).render(<Landing />)\n',
    )
    _write(tmp_path / "frontend/src/Landing.jsx", _component("Landing"))
    assert audit_spa_mount(tmp_path) == []


def test_tests_and_stories_are_not_flagged_as_pages(tmp_path: Path):
    _write(
        tmp_path / "frontend/index.html",
        '<script type="module" src="/src/main.jsx"></script>',
    )
    _write(
        tmp_path / "frontend/src/main.jsx",
        'import App from "./App.jsx"\n'
        'ReactDOM.createRoot(document.getElementById("root")).render(<App />)\n',
    )
    _write(tmp_path / "frontend/src/App.jsx", "export default function App() { return null }\n")
    # unreachable, but excluded: tests / stories / d.ts are not mount points
    _write(tmp_path / "frontend/src/pages/Auth.test.jsx", "// test\n")
    _write(tmp_path / "frontend/src/pages/Auth.stories.jsx", "// story\n")
    assert audit_spa_mount(tmp_path) == []


def test_dependencies_and_virtualenvs_are_pruned_not_scanned(tmp_path: Path):
    """A dependency's pages/ or a .venv App must never be flagged (or walked):
    node_modules/.venv are pruned from the walk, not just filtered from results."""
    _write(
        tmp_path / "frontend/index.html",
        '<script type="module" src="/src/main.jsx"></script>',
    )
    _write(
        tmp_path / "frontend/src/main.jsx",
        'import App from "./App.jsx"\n'
        'ReactDOM.createRoot(document.getElementById("root")).render(<App />)\n',
    )
    _write(tmp_path / "frontend/src/App.jsx", _component("App"))
    # dependency / venv traps: pages + an App shell that must be ignored
    _write(tmp_path / "frontend/node_modules/some-lib/pages/Auth.jsx", _component("Auth"))
    _write(tmp_path / ".venv/Lib/site-packages/framework/pages/Home.jsx", _component("Home"))
    _write(tmp_path / ".venv/Lib/site-packages/framework/App.jsx", _component("App"))
    _write(tmp_path / "frontend/dist/bundle/App.jsx", _component("App"))
    assert audit_spa_mount(tmp_path) == []


def _entry(root: Path, main_body: str) -> None:
    _write(
        root / "frontend/index.html",
        '<script type="module" src="/src/main.jsx"></script>',
    )
    _write(root / "frontend/src/main.jsx", main_body)


def test_app_reachable_via_a_barrel_reexport_is_not_flagged(tmp_path: Path):
    # App imported through components/index.js re-export: wired, not dead
    _entry(tmp_path, 'import App from "./components/index.js"\nrender(<App/>)\n')
    _write(tmp_path / "frontend/src/components/index.js", 'export { default } from "./App.jsx"\n')
    _write(tmp_path / "frontend/src/components/App.jsx", _component("App"))
    assert audit_spa_mount(tmp_path) == []


def test_app_reachable_via_extensionless_import_is_not_flagged(tmp_path: Path):
    # import App from "./App" (no extension): resolver tries known suffixes
    _entry(tmp_path, 'import App from "./App"\nrender(<App/>)\n')
    _write(tmp_path / "frontend/src/App.jsx", _component("App"))
    assert audit_spa_mount(tmp_path) == []


def test_an_unmounted_app_with_no_import_at_all_is_flagged(tmp_path: Path):
    # entry renders markup directly; App.jsx is never referenced anywhere
    _entry(tmp_path, 'ReactDOM.createRoot(document.getElementById("root")).render(<div>hi</div>)\n')
    _write(tmp_path / "frontend/src/App.jsx", _component("App"))
    gaps = audit_spa_mount(tmp_path)
    assert gaps and "frontend/src/App.jsx" in "\n".join(gaps)


def test_wildcard_and_named_reexports_are_followed(tmp_path: Path):
    # export * from "./App" (wildcard barrel): wired, must not be flagged
    _entry(tmp_path, 'import App from "./components/index"\nrender(<App/>)\n')
    _write(tmp_path / "frontend/src/components/index.js", 'export * from "./App"\n')
    _write(tmp_path / "frontend/src/components/App.jsx", _component("App"))
    assert audit_spa_mount(tmp_path) == []


def test_typescript_type_import_is_followed(tmp_path: Path):
    # import type { X } from "./types": a type-only import still wires the module
    _entry(tmp_path, 'import App from "./App"\nimport type { P } from "./types"\nrender(<App/>)\n')
    _write(tmp_path / "frontend/src/App.tsx", _component("App"))
    assert audit_spa_mount(tmp_path) == []


def test_lazy_loaded_pages_count_as_reachable(tmp_path: Path):
    # React.lazy(dynamic import) pages are wired, not dead — must not be flagged
    _write(
        tmp_path / "frontend/index.html",
        '<script type="module" src="/src/main.jsx"></script>',
    )
    _write(
        tmp_path / "frontend/src/main.jsx",
        'import React, { lazy } from "react"\n'
        'const Dashboard = lazy(() => import("./pages/Dashboard.jsx"))\n'
        'ReactDOM.createRoot(document.getElementById("root")).render(<Dashboard />)\n',
    )
    _write(tmp_path / "frontend/src/pages/Dashboard.jsx", _component("Dashboard"))
    assert audit_spa_mount(tmp_path) == []
