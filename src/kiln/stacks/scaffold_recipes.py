"""Allowlisted scaffold recipes: the deterministic way a root gets its skeleton.

Agents never freestyle a create CLI. The companion looks a framework up here
and either runs a pinned official argv (Vite / Next / Nest / Express) or writes
a pinned template tree (FastAPI / Flask — those ecosystems have no stable
non-interactive create command). Every command is an argv list run without a
shell, so there is no quoting to get wrong and the approval prompt shows
exactly what executes.

Two recipe kinds:

- ``argv``: run an official create CLI, pinned to a scaffolder version, with
  interactivity killed (stdin closed, CI=1, --yes/--no-interactive flags).
- ``template``: the companion writes a small, official-shaped tree. Honest
  note: template recipes produce Kiln-pinned files, not vendor CLI output, so
  they have no "official fingerprint" — the eval checks them for low token
  spend instead.

Adding a framework: add the recipe here FIRST (pinned, with a manifest and a
dirty-refusal rule), THEN add the matching skill pack. Bump a scaffolder
version and you re-run the scaffold eval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

RecipeKind = Literal["argv", "template"]

# Closed option enums per recipe — never a free-form shell from the model.
PACKAGE_MANAGERS = ("npm",)


@dataclass(frozen=True)
class Recipe:
    id: str
    kind: RecipeKind
    # file whose presence marks the root as already scaffolded (dirty-refusal)
    manifest: str
    # argv recipes: the pinned create command. "{root}" is substituted.
    argv: tuple[str, ...] = ()
    # template recipes: (relative_path, content) pairs the companion writes.
    template_files: tuple[tuple[str, str], ...] = ()
    # human note shown in the approval prompt
    note: str = ""
    # closed option set the model may pass
    allowed_options: frozenset[str] = frozenset()


_FASTAPI_MAIN = '''"""Application entry point. Routers are wired here, nowhere else."""

from fastapi import FastAPI

from app.routers import health

app = FastAPI(title="app")
app.include_router(health.router)
'''

_FASTAPI_HEALTH = '''"""Liveness. Cheap: no dependency fan-out, registered before auth."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
'''

_FLASK_APP = '''"""Application factory. Blueprints are registered here, nowhere else."""

from flask import Flask, jsonify


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():  # cheap liveness, no dependency fan-out
        return jsonify(status="ok")

    return app
'''

_EXPRESS_INDEX = '''import express from "express";

const app = express();
app.use(express.json());

// cheap liveness, registered before any auth middleware
app.get("/health", (_req, res) => {
  res.status(200).json({ status: "ok" });
});

const port = process.env.PORT || 3000;
app.listen(port, () => {
  console.log(`listening on ${port}`);
});
'''


RECIPES: dict[str, Recipe] = {
    "vite_react": Recipe(
        id="vite_react",
        kind="argv",
        manifest="vite.config.ts",
        argv=(
            "npm", "create", "vite@6", "{root}", "--",
            "--template", "react-ts",
        ),
        note="scaffold a Vite + React (TypeScript) SPA",
        allowed_options=frozenset({"package_manager"}),
    ),
    "nextjs": Recipe(
        id="nextjs",
        kind="argv",
        manifest="next.config.ts",
        argv=(
            "npx", "--yes", "create-next-app@15", "{root}",
            "--ts", "--eslint", "--tailwind", "--app",
            "--use-npm", "--yes", "--disable-git",
        ),
        note="scaffold a Next.js (App Router, TypeScript, Tailwind) app",
        allowed_options=frozenset({"package_manager"}),
    ),
    "nestjs": Recipe(
        id="nestjs",
        kind="argv",
        manifest="nest-cli.json",
        argv=(
            "npx", "--yes", "@nestjs/cli@10", "new", "{root}",
            "--package-manager", "npm", "--skip-git",
        ),
        note="scaffold a NestJS project via the official CLI",
        allowed_options=frozenset({"package_manager"}),
    ),
    "express": Recipe(
        id="express",
        kind="template",
        manifest="package.json",
        template_files=(
            ("package.json", '{\n  "name": "api",\n  "private": true,\n  "type": "module",\n  "scripts": {\n    "start": "node src/index.js"\n  },\n  "dependencies": {\n    "express": "^4.19.0"\n  }\n}\n'),
            ("src/index.js", _EXPRESS_INDEX),
        ),
        note="write a pinned ESM Express entry + manifest (no interactive init)",
    ),
    "fastapi": Recipe(
        id="fastapi",
        kind="template",
        manifest="app/main.py",
        template_files=(
            ("app/__init__.py", ""),
            ("app/main.py", _FASTAPI_MAIN),
            ("app/routers/__init__.py", ""),
            ("app/routers/health.py", _FASTAPI_HEALTH),
            ("requirements.txt", "fastapi\nuvicorn[standard]\n"),
        ),
        note="write a pinned FastAPI app package (no official create CLI exists)",
    ),
    "flask": Recipe(
        id="flask",
        kind="template",
        manifest="app/__init__.py",
        template_files=(
            ("app/__init__.py", _FLASK_APP),
            ("requirements.txt", "flask\n"),
        ),
        note="write a pinned Flask app-factory package",
    ),
    # django is admitted in the schema enum but deferred: it has no Kiln test
    # profile yet, so shipping a scaffold without a suite gate would be noise.
}


# The minimum a developer must write by hand when a root's official scaffold
# cannot run (it failed, or the user declined it): the manifest and the config
# the framework will not start without. Paths are relative to the root.
SKELETON_FILES: dict[str, tuple[str, ...]] = {
    "nextjs": ("package.json", "next.config.ts", "tsconfig.json", "next-env.d.ts"),
    "vite_react": ("package.json", "vite.config.ts", "tsconfig.json", "index.html"),
    "nestjs": ("package.json", "nest-cli.json", "tsconfig.json"),
    "express": ("package.json",),
    "fastapi": ("requirements.txt", "app/__init__.py", "app/main.py"),
    "flask": ("requirements.txt", "app/__init__.py"),
}


def skeleton_files(framework: str) -> tuple[str, ...]:
    return SKELETON_FILES.get(framework, ())


def recipe_for(framework: str) -> Recipe:
    try:
        return RECIPES[framework]
    except KeyError:
        known = ", ".join(sorted(RECIPES))
        raise ValueError(
            f"no scaffold recipe for framework {framework!r}; supported: {known}"
        ) from None


def npm_package_name(folder_name: str) -> str:
    """An npm-valid package name derived from a folder name.

    Create CLIs (create-next-app, create-vite, nest new) name the package after
    their target folder and refuse names npm rejects — capitals above all, so a
    workspace called ``Notescalidraw`` could never be scaffolded in place
    (run-9bbe61f0). Lowercase, keep ``[a-z0-9._-]``, collapse the rest to
    ``-``, never start with ``.`` or ``_``, cap at 214 chars.
    """
    name = re.sub(r"[^a-z0-9._-]+", "-", folder_name.strip().lower())
    name = re.sub(r"-{2,}", "-", name).strip("-._")
    return name[:214] or "app"


def scaffold_argv(framework: str, root: str, options: dict | None = None) -> list[str]:
    """The pinned argv for an ``argv`` recipe, with ``{root}`` substituted.

    Raises ValueError for template recipes (they write files, not commands) and
    for any option outside the recipe's closed set.
    """
    recipe = recipe_for(framework)
    if recipe.kind != "argv":
        raise ValueError(f"{framework} is a template recipe; it writes files, not argv")
    options = dict(options or {})
    unknown = set(options) - set(recipe.allowed_options) - {"overwrite"}
    if unknown:
        raise ValueError(f"{framework}: unsupported scaffold options {sorted(unknown)}")
    return [part.replace("{root}", root) for part in recipe.argv]


def scaffold_files(framework: str, root: str) -> list[tuple[str, str]]:
    """The (path, content) pairs a ``template`` recipe writes under ``root``."""
    recipe = recipe_for(framework)
    if recipe.kind != "template":
        raise ValueError(f"{framework} is an argv recipe; it runs a command")
    prefix = "" if root in ("", ".") else root.rstrip("/") + "/"
    return [(prefix + path, content) for path, content in recipe.template_files]
