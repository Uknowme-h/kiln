"""Mechanical product checklist for a finished Kiln workspace.

Usage:
  python -m evals.checklist <workspace> [--idea "..."] [--out path.json]

Reads docs/discovery.md (and optional idea), runs served_ui_audit, and checks
presence of common prod/security artifacts. Writes a JSON report — never an
LLM judge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running as `python -m evals.checklist` from the repo root.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kiln.agents.skills import (  # noqa: E402
    needs_prod_ops_skill,
    needs_security_skill,
    needs_ui_product_skill,
)
from kiln.product.served_ui_audit import audit_served_ui  # noqa: E402


def _read(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def run_checklist(
    workspace: Path,
    *,
    idea: str = "",
) -> dict:
    workspace = Path(workspace)
    discovery = _read(workspace / "docs" / "discovery.md")
    rows: list[dict] = []

    gaps = audit_served_ui(workspace, idea=idea, discovery=discovery)
    rows.append(
        {
            "id": "served_ui_audit",
            "pass": not gaps,
            "detail": gaps or ["cleared"],
        }
    )

    want_ui = needs_ui_product_skill(idea=idea, discovery=discovery)
    want_ops = needs_prod_ops_skill(idea=idea, discovery=discovery)
    want_sec = needs_security_skill(idea=idea, discovery=discovery)

    if want_ui:
        design_ok = (workspace / "docs" / "design.md").is_file()
        rows.append(
            {
                "id": "design_md",
                "pass": design_ok,
                "detail": ["docs/design.md present" if design_ok else "missing docs/design.md"],
            }
        )

    if want_ops:
        env_ok = (workspace / ".env.example").is_file()
        rows.append(
            {
                "id": "env_example",
                "pass": env_ok,
                "detail": [
                    ".env.example present" if env_ok else "missing .env.example"
                ],
            }
        )
        readme_ok = (workspace / "README.md").is_file()
        rows.append(
            {
                "id": "readme",
                "pass": readme_ok,
                "detail": ["README.md present" if readme_ok else "missing README.md"],
            }
        )

    if want_sec:
        # Soft check: bcrypt/argon2/scrypt somewhere in the tree (excluding node_modules).
        blob_hits = 0
        for path in workspace.rglob("*"):
            if not path.is_file():
                continue
            if "node_modules" in path.parts or ".git" in path.parts:
                continue
            if path.suffix.lower() not in {".py", ".js", ".ts", ".mjs", ".cjs"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                continue
            if any(tok in text for tok in ("bcrypt", "argon2", "scrypt", "pbkdf2")):
                blob_hits += 1
                break
        rows.append(
            {
                "id": "password_hashing",
                "pass": blob_hits > 0,
                "detail": [
                    "found hash library reference"
                    if blob_hits
                    else "no bcrypt/argon2/scrypt/pbkdf2 reference found"
                ],
            }
        )

    agents_ok = (workspace / "AGENTS.md").is_file()
    rows.append(
        {
            "id": "agents_md",
            "pass": agents_ok,
            "detail": ["AGENTS.md present" if agents_ok else "missing AGENTS.md (optional)"],
            "optional": True,
        }
    )

    required = [r for r in rows if not r.get("optional")]
    return {
        "workspace": str(workspace.resolve()),
        "idea": idea,
        "flags": {
            "ui_product": want_ui,
            "prod_ops": want_ops,
            "security": want_sec,
        },
        "pass": all(r["pass"] for r in required),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--idea", default="")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write JSON here (default: stdout)",
    )
    args = parser.parse_args(argv)
    result = run_checklist(args.workspace, idea=args.idea)
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
