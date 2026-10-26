"""Paths agents are allowed to write, always relative to the generated workspace."""

from pathlib import PurePosixPath


def safe_relative_path(path: str) -> str:
    """Return a normalized workspace-relative path, or raise ValueError."""
    if not isinstance(path, str) or path == "" or path != path.strip():
        raise ValueError("path must be a non-empty relative path")
    posix = path.replace("\\", "/")
    if posix.startswith("/"):
        raise ValueError(f"path must stay inside the workspace: {path}")
    parts = PurePosixPath(posix).parts
    if not parts or ":" in parts[0] or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"path must stay inside the workspace: {path}")
    return "/".join(parts)


def paths_conflict(left: str, right: str) -> bool:
    """True when two owned paths are the same file or one contains the other."""
    a = safe_relative_path(left)
    b = safe_relative_path(right)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")
