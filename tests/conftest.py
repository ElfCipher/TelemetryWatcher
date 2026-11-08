from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from telemetry.api import create_app
from telemetry.collector import MetricsCollector
from telemetry.config import Settings
from telemetry.models import MetricPoint
from telemetry.storage import MetricsStorage


@pytest.fixture
def make_point():
    """Build a valid metric point with sensible defaults."""

    def factory(
        timestamp: float = 1000.0,
        cpu_percent: float = 12.5,
        per_cpu: Optional[List[float]] = None,
        load_avg: Optional[List[float]] = None,
        temperature_c: Optional[float] = 48.2,
        mem_total: float = 3936.0,
        mem_available: float = 2100.0,
        mem_percent: float = 46.7,
        disk_total: float = 29600.0,
        disk_used: float = 12000.0,
        disk_percent: float = 40.5,
        read_bytes: int = 123456,
        write_bytes: int = 654321,
        net_sent: int = 987654,
        net_recv: int = 1234567,
        packets_sent: int = 1000,
        packets_recv: int = 1500,
        processes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        point = MetricPoint(
            timestamp=float(timestamp),
            cpu={
                "percent": cpu_percent,
                "per_cpu": per_cpu if per_cpu is not None else [10.0, 15.0],
                "load_avg": load_avg if load_avg is not None else [0.5, 0.4, 0.3],
            },
            temperature_c=temperature_c,
            memory={
                "total": mem_total,
                "available": mem_available,
                "percent": mem_percent,
            },
            disk={
                "total": disk_total,
                "used": disk_used,
                "percent": disk_percent,
                "read_bytes": read_bytes,
                "write_bytes": write_bytes,
            },
            network={
                "bytes_sent": net_sent,
                "bytes_recv": net_recv,
                "packets_sent": packets_sent,
                "packets_recv": packets_recv,
            },
            top_processes=processes
            if processes is not None
            else [
                {
                    "pid": 1234,
                    "name": "python3",
                    "cpu_percent": 25.0,
                    "memory_percent": 5.2,
                },
                {
                    "pid": 5678,
                    "name": "node",
                    "cpu_percent": 10.0,
                    "memory_percent": 8.1,
                },
            ],
        )
        return point.model_dump()

    return factory


@pytest.fixture
def settings() -> Settings:
    return Settings(interval_sec=2.0, buffer_size=50, start_collector=False)


@pytest.fixture
def storage() -> MetricsStorage:
    return MetricsStorage(maxlen=50)


@pytest.fixture
def collector(storage: MetricsStorage) -> MetricsCollector:
    return MetricsCollector(storage, interval_sec=2.0)


@pytest.fixture
def app(settings, storage, collector):
    return create_app(
        settings,
        storage=storage,
        collector=collector,
        start_background=False,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seed(client, storage):
    def _seed(points):
        for point in points:
            client.portal.call(storage.add, point)

    return _seed


@pytest.fixture
def seeded_client(client, seed, make_point):
    seed([make_point(timestamp=1000.0 + index) for index in range(5)])
    return client
