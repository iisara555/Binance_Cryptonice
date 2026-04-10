from pathlib import Path

from project_paths import resolve_project_python


def test_resolve_project_python_prefers_local_venv(tmp_path: Path):
    python_path = tmp_path / ".venv-3" / "Scripts" / "python.exe"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("", encoding="utf-8")

    resolved = resolve_project_python(tmp_path, fallback_to_current=False)

    assert resolved == python_path.resolve()


def test_resolve_project_python_returns_none_without_candidates(tmp_path: Path):
    resolved = resolve_project_python(tmp_path, fallback_to_current=False)

    assert resolved is None