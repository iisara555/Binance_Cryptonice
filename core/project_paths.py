from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent


def _iter_python_candidates(project_root: Path) -> Iterable[Path]:
    for env_name in (".venv-3", ".venv", "venv"):
        yield project_root / env_name / "Scripts" / "python.exe"
        yield project_root / env_name / "bin" / "python"


def resolve_project_python(
    project_root: Optional[Path | str] = None,
    *,
    fallback_to_current: bool = True,
) -> Optional[Path]:
    """Resolve a Python executable that belongs to this project.

    Prefer a project-local virtual environment so the project remains movable
    across folders or drives without depending on an old absolute path.
    """
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT

    for candidate in _iter_python_candidates(root):
        if candidate.exists():
            return candidate.resolve()

    if fallback_to_current:
        current_python = Path(sys.executable)
        if current_python.exists():
            return current_python.resolve()

    return None
