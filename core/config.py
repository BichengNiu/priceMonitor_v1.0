"""Validated runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from urllib.parse import urlparse


_ALLOWED_KEYS = {
    "source_url",
    "data_dir",
    "log_level",
    "page_size",
    "connect_timeout_seconds",
    "read_timeout_seconds",
    "max_attempts",
}
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True)
class Settings:
    """Configuration required for one monitor run."""

    source_url: str
    data_dir: Path
    log_level: str
    page_size: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_attempts: int

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout_seconds, self.read_timeout_seconds)


def load_settings(path: Path) -> Settings:
    """Load and validate a TOML configuration file."""

    config_path = path.expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with config_path.open("rb") as handle:
        document = tomllib.load(handle)

    monitor = document.get("monitor")
    if not isinstance(monitor, dict):
        raise ValueError("Configuration must contain a [monitor] table")

    unknown = sorted(set(monitor) - _ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"Unknown monitor configuration keys: {', '.join(unknown)}")

    source_url = _required_string(monitor, "source_url")
    parsed_url = urlparse(source_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "www.moet.gov.ae":
        raise ValueError("source_url must use https://www.moet.gov.ae")

    data_dir_value = _required_string(monitor, "data_dir")
    data_dir = Path(data_dir_value).expanduser()
    if not data_dir.is_absolute():
        data_dir = config_path.parent / data_dir
    data_dir = data_dir.resolve()

    log_level = _required_string(monitor, "log_level").upper()
    if log_level not in _LOG_LEVELS:
        raise ValueError(f"log_level must be one of: {', '.join(sorted(_LOG_LEVELS))}")

    page_size = _bounded_int(monitor, "page_size", minimum=1, maximum=50_000)
    max_attempts = _bounded_int(monitor, "max_attempts", minimum=1, maximum=5)
    connect_timeout = _bounded_number(
        monitor, "connect_timeout_seconds", minimum=0.1, maximum=300
    )
    read_timeout = _bounded_number(
        monitor, "read_timeout_seconds", minimum=0.1, maximum=600
    )

    return Settings(
        source_url=source_url.rstrip("/"),
        data_dir=data_dir,
        log_level=log_level,
        page_size=page_size,
        connect_timeout_seconds=connect_timeout,
        read_timeout_seconds=read_timeout,
        max_attempts=max_attempts,
    )


def _required_string(values: dict[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _bounded_int(
    values: dict[str, object], key: str, *, minimum: int, maximum: int
) -> int:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _bounded_number(
    values: dict[str, object], key: str, *, minimum: float, maximum: float
) -> float:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return number
