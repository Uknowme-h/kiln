"""Prompt templates for the roles. One text file per role, loaded once."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """Return the template for a role's prompt file, e.g. ``requirements``."""
    if not name.isidentifier():
        raise ValueError(f"invalid prompt name: {name!r}")
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"no prompt template named {name!r}")
    return path.read_text(encoding="utf-8")
