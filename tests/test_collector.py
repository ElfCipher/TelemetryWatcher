from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import telemetry.collector as collector_module
from telemetry.collector import MetricsBroadcaster, MetricsCollector
from telemetry.storage import MetricsStorage


class FakePsutil:
    def __init__(self) -> None:
        self.cpu = 12.5
        self.per_cpu = [10.0, 15.0, 12.0, 13.0]
        self.memory = SimpleNamespace(
            total=4 * 1024**3, available=2 * 1024**3, percent=50.0
        )
        self.disk = SimpleNamespace(
            total=40 * 1024**2, used=10 * 1024**2, percent=25.0
        )
        self.disk_io = SimpleNamespace(read_bytes=123456, write_bytes=654321)
        self.net = SimpleNamespace(
            bytes_sent=987654,
            bytes_recv=1234567,
            packets_sent=1000,
            packets_recv=1500,
        )
        self.temperatures: Dict[str, List[Any]] = {}
        self.processes: List[Any] = []
        self.fail: set = set()

    def _check(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"{name} unavailable")

    def cpu_percent(self, interval=None, percpu=False):
        self._check("cpu")
        return list(self.per_cpu) if percpu else self.cpu

    def virtual_memory(self):
        self._check("memory")
        return self.memory

    def disk_usage(self, path):
        self._check("disk")
        return self.disk

    def disk_io_counters(self):
        self._check("disk_io")
        return self.disk_io

    def net_io_counters(self):
        self._check("net")
        return self.net

    def sensors_temperatures(self):
        self._check("temperature")
        return self.temperatures

    def process_iter(self, attrs=None):
        self._check("processes")
        for item in self.processes:
            if isinstance(item, BaseException):
                raise item
            yield SimpleNamespace(info=item)


@pytest.fixture
def fake_psutil(monkeypatch):
    fake = FakePsutil()
    monkeypatch.setattr(collector_module, "psutil", fake)
    return fake


def test_collect_full_point(fake_psutil, tmp_path):
    fake_psutil.processes = [
        {"pid": 1, "name": "one", "cpu_percent": 5.0, "memory_percent": 1.0},
        {"pid": 2, "name": "two", "cpu_percent": 50.0, "memory_percent": 2.0},
        {"pid": 3, "name": "three", "cpu_percent": 20.0, "memory_percent": 9.0},
        {"pid": 4, "name": "four", "cpu_percent": 10.0, "memory_percent": 3.0},
        {"pid": 5, "name": "five", "cpu_percent": 30.0, "memory_percent": 4.0},
        {"pid": 6, "name": "six", "cpu_percent": 40.0, "memory_percent": 5.0},
        {"pid": 7, "name": None, "cpu_percent": 1.0, "memory_percent": 0.5},
    ]
    thermal = tmp_path / "temp"
    thermal.write_text("48200\n", encoding="ascii")

    storage = MetricsStorage()
    collector = MetricsCollector(
        storage, thermal_path=thermal, clock=lambda: 1234.5
    )
    point = collector.collect()

    assert point["timestamp"] == 1234.5
    assert point["cpu"]["percent"] == 12.5
    assert point["cpu"]["per_cpu"] == [10.0, 15.0, 12.0, 13.0]
    assert len(point["cpu"]["load_avg"]) == 3
    assert point["temperature_c"] == 48.2
    assert point["memory"] == {
        "total": 4096.0,
        "available": 2048.0,
        "percent": 50.0,
    }
    assert point["disk"]["total"] == 40.0
    assert point["disk"]["used"] == 10.0
    assert point["disk"]["percent"] == 25.0
    assert point["disk"]["read_bytes"] == 123456
    assert point["disk"]["write_bytes"] == 654321
    assert point["network"] == {
        "bytes_sent": 987654,
        "bytes_recv": 1234567,
        "packets_sent": 1000,
        "packets_recv": 1500,
    }
    assert len(point["top_processes"]) == 5
    assert [process["pid"] for process in point["top_processes"]] == [2, 6, 5, 3, 4]


def test_temperature_from_psutil(fake_psutil, tmp_path):
    fake_psutil.temperatures = {
        "cpu_thermal": [SimpleNamespace(current=55.5)],
    }
    collector = MetricsCollector(
        MetricsStorage(), thermal_path=tmp_path / "missing"
    )
    assert collector.collect()["temperature_c"] == 55.5


def test_temperature_none_when_unavailable(fake_psutil, tmp_path):
    collector = MetricsCollector(
        MetricsStorage(), thermal_path=tmp_path / "missing"
    )
    assert collector.collect()["temperature_c"] is None


def test_invalid_sysfs_falls_back_to_psutil(fake_psutil, tmp_path):
    fake_psutil.temperatures = {
        "cpu_thermal": [SimpleNamespace(current=42.0)],
    }
    thermal = tmp_path / "temp"
    thermal.write_text("garbage\n", encoding="ascii")
    collector = MetricsCollector(MetricsStorage(), thermal_path=thermal)
    assert collector.collect()["temperature_c"] == 42.0


def test_collect_graceful_fallbacks(fake_psutil, tmp_path):
    fake_psutil.fail = {
        "cpu",
        "memory",
        "disk",
        "disk_io",
        "net",
        "temperature",
        "processes",
    }
    collector = MetricsCollector(
        MetricsStorage(), thermal_path=tmp_path / "missing"
    )
    point = collector.collect()
    assert point["cpu"]["percent"] == 0.0
    assert point["cpu"]["per_cpu"] == []
    assert point["memory"] == {"total": 0.0, "available": 0.0, "percent": 0.0}
    assert point["disk"]["total"] == 0.0
    assert point["disk"]["read_bytes"] == 0
    assert point["network"]["bytes_sent"] == 0
    assert point["temperature_c"] is None
    assert point["top_processes"] == []


def test_process_exception_is_ignored(fake_psutil):
    fake_psutil.processes = [RuntimeError("access denied")]
    collector = MetricsCollector(MetricsStorage())
    assert collector.collect()["top_processes"] == []


def test_load_average_fallback(fake_psutil, monkeypatch):
    def boom():
        raise OSError("no loadavg")

    monkeypatch.setattr(collector_module.os, "getloadavg", boom)
    collector = MetricsCollector(MetricsStorage())
    assert collector.collect()["cpu"]["load_avg"] == [0.0, 0.0, 0.0]


async def test_set_interval():
    collector = MetricsCollector(MetricsStorage(), interval_sec=2.0)
    assert await collector.set_interval(5) == 5.0
    assert collector.interval_sec == 5.0
    with pytest.raises(ValueError):
        await collector.set_interval(0.01)
    with pytest.raises(ValueError):
        await collector.set_interval(100000)
    with pytest.raises(ValueError):
        await collector.set_interval(float("nan"))


async def test_scheduler_collects_and_stops(make_point):
    storage = MetricsStorage(maxlen=50)
    await storage.open()
    counter = itertools.count()

    def fake_collect():
        return make_point(timestamp=1000.0 + next(counter))

    collector = MetricsCollector(
        storage,
        interval_sec=0.02,
        min_interval_sec=0.01,
        collect_func=fake_collect,
    )
    await collector.start()
    assert collector.is_running
    await asyncio.sleep(0.09)
    await collector.stop()
    assert not collector.is_running

    points = await storage.snapshot()
    assert len(points) >= 2
    assert [point["timestamp"] for point in points] == sorted(
        point["timestamp"] for point in points
    )
    await storage.close()


async def test_scheduler_handles_collect_errors(make_point):
    storage = MetricsStorage(maxlen=10)
    await storage.open()
    calls = itertools.count()

    def flaky_collect():
        index = next(calls)
        if index == 0:
            raise RuntimeError("boom")
        return make_point(timestamp=2000.0 + index)

    collector = MetricsCollector(
        storage,
        interval_sec=0.02,
        min_interval_sec=0.01,
        collect_func=flaky_collect,
    )
    await collector.start()
    await asyncio.sleep(0.08)
    await collector.stop()
    assert len(await storage.snapshot()) >= 1
    await storage.close()


async def test_broadcaster_fanout_and_drop():
    broadcaster = MetricsBroadcaster(queue_size=1)
    async with broadcaster.subscribe() as queue:
        assert broadcaster.subscriber_count == 1
        broadcaster.publish({"timestamp": 1.0})
        broadcaster.publish({"timestamp": 2.0})
        assert (await queue.get())["timestamp"] == 2.0
    assert broadcaster.subscriber_count == 0


async def test_collector_publish_forwards_to_subscribers(make_point):
    collector = MetricsCollector(MetricsStorage())
    point = make_point(timestamp=77.0)
    async with collector.broadcaster.subscribe() as queue:
        collector.publish(point)
        assert (await queue.get())["timestamp"] == 77.0
