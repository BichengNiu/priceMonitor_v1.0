from __future__ import annotations

from pathlib import Path

import pytest

import run_monitor
from run_monitor import _cleanup_temporary_artifacts
from core import __version__
from core.config import load_settings
from core.service import RunResult


def _write_config(path: Path, **overrides: object) -> Path:
    values: dict[str, object] = {
        "source_url": "https://www.moet.gov.ae/en/essential-goods-prices-platform",
        "data_dir": "data",
        "log_level": "INFO",
        "page_size": 5000,
        "connect_timeout_seconds": 10,
        "read_timeout_seconds": 60,
        "max_attempts": 3,
    }
    values.update(overrides)
    lines = ["[monitor]"]
    for key, value in values.items():
        rendered = f'"{value}"' if isinstance(value, str) else str(value).lower()
        lines.append(f"{key} = {rendered}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_settings_resolves_paths_and_values(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path / "config.toml")

    settings = load_settings(config_path)

    assert settings.data_dir == (tmp_path / "data").resolve()
    assert settings.page_size == 5000
    assert settings.timeout == (10.0, 60.0)
    assert settings.log_level == "INFO"
    assert settings.max_attempts == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_url", "http://example.com"),
        ("page_size", 0),
        ("page_size", 50001),
        ("connect_timeout_seconds", 0),
        ("read_timeout_seconds", -1),
        ("max_attempts", 0),
        ("max_attempts", 6),
        ("log_level", "VERBOSE"),
    ],
)
def test_load_settings_rejects_unsafe_values(
    tmp_path: Path, field: str, value: object
) -> None:
    config_path = _write_config(tmp_path / "config.toml", **{field: value})

    with pytest.raises(ValueError):
        load_settings(config_path)


def test_required_delivery_files_and_commands_are_present() -> None:
    root = Path(__file__).resolve().parents[1]
    required = {
        "README.md",
        "requirements.txt",
        "config.example.toml",
        "run_monitor.py",
        "build.ps1",
        "install_task.ps1",
    }

    assert required <= {path.name for path in root.iterdir() if path.is_file()}

    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "requests>=2.34.2,<3" in requirements
    assert "pandas>=2.3,<4" in requirements
    assert "pytest>=9.1.1,<10" in requirements

    readme = (root / "README.md").read_text(encoding="utf-8")
    for command in (
        ".\\build.ps1",
        "python run_monitor.py --config config.toml",
        ".\\release\\MoetPriceMonitor.exe --config .\\config.toml",
        ".\\install_task.ps1",
    ):
        assert command in readme
    assert __version__ == "1.0.0"


def test_cli_returns_zero_after_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _write_config(tmp_path / "config.toml")
    source_version = "2026-08-10T00:00:00+04:00"

    class SuccessfulMonitor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def run_once(self) -> RunResult:
            from datetime import datetime

            return RunResult(datetime.fromisoformat(source_version), False, 0, 0)

    monkeypatch.setattr(run_monitor, "PriceMonitor", SuccessfulMonitor)

    assert run_monitor.main(["--config", str(config_path)]) == 0


def test_cli_returns_one_and_logs_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path / "config.toml")

    class FailingMonitor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def run_once(self) -> RunResult:
            raise RuntimeError("planned failure")

    monkeypatch.setattr(run_monitor, "PriceMonitor", FailingMonitor)

    assert run_monitor.main(["--config", str(config_path)]) == 1
    log_text = (tmp_path / "logs" / "monitor.log").read_text(encoding="utf-8")
    assert "planned failure" in log_text


def test_success_cleanup_removes_owned_artifacts_but_keeps_runtime_files(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    data_dir = project_dir / "data"
    (project_dir / ".build").mkdir(parents=True)
    (project_dir / ".build-venv").mkdir()
    (project_dir / ".pytest-tmp-run").mkdir()
    (project_dir / "core" / "__pycache__").mkdir(parents=True)
    (project_dir / "core" / "cached.pyc").write_bytes(b"cache")
    data_dir.mkdir()
    (data_dir / ".prices_panel.pending.dta").write_bytes(b"pending")
    (project_dir / "release").mkdir()
    (project_dir / "release" / "MoetPriceMonitor.exe").write_bytes(b"runtime")

    _cleanup_temporary_artifacts(project_dir, data_dir)

    assert not (project_dir / ".build").exists()
    assert not (project_dir / ".build-venv").exists()
    assert not (project_dir / ".pytest-tmp-run").exists()
    assert not (project_dir / "core" / "__pycache__").exists()
    assert not (project_dir / "core" / "cached.pyc").exists()
    assert not (data_dir / ".prices_panel.pending.dta").exists()
    assert (project_dir / "release" / "MoetPriceMonitor.exe").exists()
