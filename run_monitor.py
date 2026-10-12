"""Command-line entry point for one MOET price-monitor run."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import stat
import sys

from core.client import MoetClient
from core.config import Settings, load_settings
from core.history import HistoryStore
from core.service import PriceMonitor


LOGGER = logging.getLogger("core")
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the MOET public price platform once and append a new panel period."
    )
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="TOML configuration file supplied by the root BAT launcher",
    )
    args = parser.parse_args(argv)
    config_path = args.config

    try:
        settings = load_settings(config_path)
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    _configure_logging(settings)
    try:
        client = MoetClient(settings)
        history = HistoryStore(settings.data_dir)
        monitor = PriceMonitor(client, history)
        result = monitor.run_once()
    except Exception:
        LOGGER.exception("Price monitor run failed")
        return 1

    _cleanup_temporary_artifacts(config_path.parent, settings.data_dir)

    if result.update_found:
        message = (
            f"Update processed: source={result.source_updated_at.isoformat()}, "
            f"fetched={result.fetched_count}, appended={result.appended_count}"
        )
    else:
        message = f"No update: source={result.source_updated_at.isoformat()}"
    LOGGER.info(message)
    print(message)
    return 0


def _configure_logging(settings: Settings) -> None:
    log_dir = settings.data_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "monitor.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.log_level))
    for handler in list(root.handlers):
        if getattr(handler, "_moet_monitor_handler", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._moet_monitor_handler = True  # type: ignore[attr-defined]

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler._moet_monitor_handler = True  # type: ignore[attr-defined]

    root.addHandler(console)
    root.addHandler(file_handler)


def _cleanup_temporary_artifacts(project_dir: Path, data_dir: Path) -> None:
    """Remove only temporary artifacts owned by this project after a success."""

    roots = {project_dir.resolve(), data_dir.resolve()}
    exact_files = {
        ".commit-transaction.json",
        ".commit-transaction.json.tmp",
        ".prices_panel.pending.duckdb",
        ".prices_panel.pending.duckdb.wal",
        ".prices_panel.pending.dta",
    }
    exact_directories = {".build", ".pytest_cache"}
    blocked_directories = {
        (project_dir.resolve() / name).resolve()
        for name in (".build-venv", ".venv")
    }

    for root in roots:
        if not root.is_dir():
            continue
        for name in exact_files:
            _remove_file(root / name)
        for path in root.iterdir():
            if path.name.startswith(".pytest-tmp"):
                _remove_path(path)

    project_dir = project_dir.resolve()
    if project_dir.is_dir():
        for name in exact_directories:
            path = project_dir / name
            if not _remove_path(path):
                blocked_directories.add(path.resolve())

        for path in project_dir.rglob("__pycache__"):
            if not _is_under_any(path, blocked_directories):
                _remove_path(path)
        for path in project_dir.rglob("*.pyc"):
            if not _is_under_any(path, blocked_directories):
                _remove_file(path)


def _is_under_any(path: Path, roots: set[Path]) -> bool:
    """Return whether path is inside one of the protected cleanup roots."""

    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.warning("Could not remove temporary file %s: %s", path, exc)


def _remove_path(path: Path) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path, onexc=_remove_readonly_and_retry)
        else:
            path.unlink(missing_ok=True)
        return not path.exists()
    except OSError as exc:
        LOGGER.warning("Could not remove temporary path %s: %s", path, exc)
        return False


def _remove_readonly_and_retry(function: object, path: str, excinfo: BaseException) -> None:
    """Allow cleanup to remove read-only files and directories on Windows."""

    if not isinstance(excinfo, PermissionError):
        raise excinfo
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    function(path)  # type: ignore[operator]


if __name__ == "__main__":
    raise SystemExit(main())
