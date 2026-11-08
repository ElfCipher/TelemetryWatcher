from __future__ import annotations

import math

import pytest

from telemetry.config import Settings


def test_defaults():
    settings = Settings()
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.interval_sec == 2.0
    assert settings.buffer_size == 3600
    assert settings.top_processes == 5
    assert settings.sqlite_path is None
    assert settings.auth_token is None
    assert settings.start_collector is True
    assert settings.frontend_dir.is_dir()
    assert (settings.frontend_dir / "index.html").is_file()


def test_from_env():
    env = {
        "TELEMETRY_HOST": "127.0.0.1",
        "TELEMETRY_PORT": "9001",
        "TELEMETRY_INTERVAL": "5",
        "TELEMETRY_BUFFER_SIZE": "100",
        "TELEMETRY_TOP_PROCESSES": "7",
        "TELEMETRY_SQLITE_PATH": "/tmp/telemetry-test.db",
        "TELEMETRY_SQLITE_FLUSH": "10",
        "TELEMETRY_TOKEN": "secret",
        "TELEMETRY_START_COLLECTOR": "false",
        "TELEMETRY_LOG_LEVEL": "debug",
    }
    settings = Settings.from_env(env)
    assert settings.host == "127.0.0.1"
    assert settings.port == 9001
    assert settings.interval_sec == 5.0
    assert settings.buffer_size == 100
    assert settings.top_processes == 7
    assert str(settings.sqlite_path) == "/tmp/telemetry-test.db"
    assert settings.sqlite_flush_sec == 10.0
    assert settings.auth_token == "secret"
    assert settings.start_collector is False
    assert settings.log_level == "debug"


@pytest.mark.parametrize("value", [0.1, 0.0, -1, 4000, math.nan, math.inf])
def test_invalid_interval_rejected(value):
    with pytest.raises(ValueError):
        Settings(interval_sec=value)


def test_validate_interval_accepts_bounds():
    settings = Settings()
    assert settings.validate_interval(0.5) == 0.5
    assert settings.validate_interval(3600) == 3600.0
    assert settings.validate_interval("2.5") == 2.5


def test_invalid_env_value():
    with pytest.raises(ValueError):
        Settings.from_env({"TELEMETRY_PORT": "not-a-number"})
    with pytest.raises(ValueError):
        Settings.from_env({"TELEMETRY_START_COLLECTOR": "maybe"})


def test_invalid_port_and_buffer():
    with pytest.raises(ValueError):
        Settings(port=0)
    with pytest.raises(ValueError):
        Settings(buffer_size=1)
    with pytest.raises(ValueError):
        Settings(top_processes=0)
