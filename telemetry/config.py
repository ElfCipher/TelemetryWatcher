"""Application settings loaded from environment variables."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_FRONTEND_DIR = PACKAGE_DIR / "frontend"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _env_str(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    return default if value is None or value == "" else value


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


@dataclass
class Settings:
    """Runtime configuration.

    Every field can be overridden through an environment variable using the
    ``TELEMETRY_`` prefix (see :meth:`from_env`).
    """

    host: str = "0.0.0.0"
    port: int = 8000
    interval_sec: float = 2.0
    min_interval_sec: float = 0.5
    max_interval_sec: float = 3600.0
    buffer_size: int = 3600
    top_processes: int = 5
    sqlite_path: Optional[Path] = None
    sqlite_flush_sec: float = 30.0
    auth_token: Optional[str] = None
    frontend_dir: Path = field(default_factory=lambda: DEFAULT_FRONTEND_DIR)
    start_collector: bool = True
    log_level: str = "info"

    def __post_init__(self) -> None:
        self.interval_sec = self.validate_interval(self.interval_sec)
        if not 1 <= int(self.port) <= 65535:
            raise ValueError(f"port must be in 1..65535, got {self.port!r}")
        if int(self.buffer_size) < 2:
            raise ValueError(f"buffer_size must be >= 2, got {self.buffer_size!r}")
        if int(self.top_processes) < 1:
            raise ValueError(f"top_processes must be >= 1, got {self.top_processes!r}")
        if not math.isfinite(float(self.sqlite_flush_sec)) or self.sqlite_flush_sec <= 0:
            raise ValueError("sqlite_flush_sec must be a positive number")
        self.frontend_dir = Path(self.frontend_dir)
        if self.sqlite_path is not None:
            self.sqlite_path = Path(self.sqlite_path)
        self.log_level = str(self.log_level).lower()

    def validate_interval(self, value: float) -> float:
        """Return a validated collection interval or raise ``ValueError``."""
        try:
            interval = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"interval_sec must be a number, got {value!r}") from exc
        if not math.isfinite(interval):
            raise ValueError("interval_sec must be a finite number")
        if interval < self.min_interval_sec or interval > self.max_interval_sec:
            raise ValueError(
                "interval_sec must be between "
                f"{self.min_interval_sec:g} and {self.max_interval_sec:g} seconds"
            )
        return interval

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        """Build settings from ``TELEMETRY_*`` environment variables."""
        source = os.environ if env is None else env
        sqlite_raw = _env_str(source, "TELEMETRY_SQLITE_PATH", "")
        token_raw = _env_str(source, "TELEMETRY_TOKEN", "")
        frontend_raw = _env_str(source, "TELEMETRY_FRONTEND_DIR", str(DEFAULT_FRONTEND_DIR))
        return cls(
            host=_env_str(source, "TELEMETRY_HOST", "0.0.0.0"),
            port=_env_int(source, "TELEMETRY_PORT", 8000),
            interval_sec=_env_float(source, "TELEMETRY_INTERVAL", 2.0),
            min_interval_sec=_env_float(source, "TELEMETRY_MIN_INTERVAL", 0.5),
            max_interval_sec=_env_float(source, "TELEMETRY_MAX_INTERVAL", 3600.0),
            buffer_size=_env_int(source, "TELEMETRY_BUFFER_SIZE", 3600),
            top_processes=_env_int(source, "TELEMETRY_TOP_PROCESSES", 5),
            sqlite_path=Path(sqlite_raw) if sqlite_raw else None,
            sqlite_flush_sec=_env_float(source, "TELEMETRY_SQLITE_FLUSH", 30.0),
            auth_token=token_raw or None,
            frontend_dir=Path(frontend_raw),
            start_collector=_env_bool(source, "TELEMETRY_START_COLLECTOR", True),
            log_level=_env_str(source, "TELEMETRY_LOG_LEVEL", "info"),
        )
