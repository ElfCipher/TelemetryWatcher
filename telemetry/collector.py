"""Metric collection from psutil/sysfs and background scheduling."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set

import psutil

from .models import MetricPoint
from .storage import MetricsStorage

logger = logging.getLogger(__name__)

DEFAULT_THERMAL_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
_MB = 1024.0 * 1024.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round(value: Any, digits: int = 1) -> float:
    return round(float(value), digits)


class MetricsBroadcaster:
    """Fan-out of metric points to WebSocket subscribers.

    Slow subscribers are dropped rather than blocking the collector.
    """

    def __init__(self, queue_size: int = 50) -> None:
        self._queue_size = max(1, int(queue_size))
        self._subscribers: Set[asyncio.Queue] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        queue = self.register()
        try:
            yield queue
        finally:
            self.unregister(queue)

    def register(self) -> asyncio.Queue:
        """Register a subscriber without using an async context manager."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, point: Dict[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(point)
            except asyncio.QueueFull:
                # Keep the freshest data for slow consumers.
                try:
                    queue.get_nowait()
                    queue.put_nowait(point)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


class MetricsCollector:
    """Collects a metric point every ``interval_sec`` and stores it."""

    def __init__(
        self,
        storage: MetricsStorage,
        interval_sec: float = 2.0,
        top_processes: int = 5,
        min_interval_sec: float = 0.5,
        max_interval_sec: float = 3600.0,
        broadcaster: Optional[MetricsBroadcaster] = None,
        thermal_path: Optional[Path] = None,
        clock: Optional[Callable[[], float]] = None,
        collect_func: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._storage = storage
        self._min_interval = float(min_interval_sec)
        self._max_interval = float(max_interval_sec)
        self._interval = self.validate_interval(interval_sec)
        self._top_processes = max(1, int(top_processes))
        self.broadcaster = broadcaster or MetricsBroadcaster()
        self._thermal_path = Path(thermal_path) if thermal_path else DEFAULT_THERMAL_PATH
        self._clock = clock or time.time
        self._collect_func = collect_func
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._stop = False

    @property
    def storage(self) -> MetricsStorage:
        return self._storage

    @property
    def interval_sec(self) -> float:
        return self._interval

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def validate_interval(self, value: float) -> float:
        try:
            interval = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"interval_sec must be a number, got {value!r}") from exc
        if not math.isfinite(interval):
            raise ValueError("interval_sec must be a finite number")
        if interval < self._min_interval or interval > self._max_interval:
            raise ValueError(
                "interval_sec must be between "
                f"{self._min_interval:g} and {self._max_interval:g} seconds"
            )
        return interval

    async def set_interval(self, value: float) -> float:
        interval = self.validate_interval(value)
        self._interval = interval
        # Wake the scheduler so the new interval takes effect immediately.
        self._wake.set()
        return interval

    def publish(self, point: Dict[str, Any]) -> None:
        """Push a point to WebSocket subscribers (used by the scheduler/tests)."""
        self.broadcaster.publish(point)

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop = False
        if self._collect_func is None:
            self._prime()
        self._task = asyncio.create_task(self._run(), name="telemetry-collector")

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stop:
            try:
                if self._collect_func is not None:
                    point = self._collect_func()
                    if inspect.isawaitable(point):
                        point = await point
                else:
                    point = await asyncio.to_thread(self.collect)
                if isinstance(point, MetricPoint):
                    point = point.model_dump()
                await self._storage.add(point)
                self.broadcaster.publish(point)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Metric collection failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            self._wake.clear()

    def _prime(self) -> None:
        """Prime psutil counters so the first point is not all zeroes."""
        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            logger.debug("Could not prime CPU counters", exc_info=True)
        try:
            for process in psutil.process_iter(attrs=["pid", "cpu_percent"]):
                _ = process.info
        except Exception:
            logger.debug("Could not prime process counters", exc_info=True)

    def collect(self) -> Dict[str, Any]:
        point = MetricPoint(
            timestamp=float(self._clock()),
            cpu=self._collect_cpu(),
            temperature_c=self._read_temperature(),
            memory=self._collect_memory(),
            disk=self._collect_disk(),
            network=self._collect_network(),
            top_processes=self._collect_processes(),
        )
        return point.model_dump()

    def _collect_cpu(self) -> Dict[str, Any]:
        percent = 0.0
        per_cpu: List[float] = []
        try:
            percent = _clamp(_round(psutil.cpu_percent(interval=None), 1), 0.0, 100.0)
        except Exception:
            logger.debug("CPU percent unavailable", exc_info=True)
        try:
            per_cpu = [
                _clamp(_round(value, 1), 0.0, 100.0)
                for value in psutil.cpu_percent(interval=None, percpu=True)
            ]
        except Exception:
            logger.debug("Per-CPU percent unavailable", exc_info=True)
        try:
            load_avg = [_round(value, 2) for value in os.getloadavg()]
        except (AttributeError, OSError):
            load_avg = [0.0, 0.0, 0.0]
        return {"percent": percent, "per_cpu": per_cpu, "load_avg": load_avg}

    def _collect_memory(self) -> Dict[str, Any]:
        try:
            memory = psutil.virtual_memory()
            return {
                "total": _round(memory.total / _MB, 1),
                "available": _round(memory.available / _MB, 1),
                "percent": _round(memory.percent, 1),
            }
        except Exception:
            logger.debug("Memory metrics unavailable", exc_info=True)
            return {"total": 0.0, "available": 0.0, "percent": 0.0}

    def _collect_disk(self) -> Dict[str, Any]:
        total = used = percent = 0.0
        read_bytes = write_bytes = 0
        try:
            usage = psutil.disk_usage(os.path.abspath(os.sep))
            total = _round(usage.total / _MB, 1)
            used = _round(usage.used / _MB, 1)
            percent = _round(usage.percent, 1)
        except Exception:
            logger.debug("Disk usage unavailable", exc_info=True)
        try:
            io_counters = psutil.disk_io_counters()
            if io_counters is not None:
                read_bytes = int(io_counters.read_bytes)
                write_bytes = int(io_counters.write_bytes)
        except Exception:
            logger.debug("Disk I/O counters unavailable", exc_info=True)
        return {
            "total": total,
            "used": used,
            "percent": percent,
            "read_bytes": read_bytes,
            "write_bytes": write_bytes,
        }

    def _collect_network(self) -> Dict[str, Any]:
        try:
            counters = psutil.net_io_counters()
        except Exception:
            logger.debug("Network counters unavailable", exc_info=True)
            counters = None
        if counters is None:
            return {
                "bytes_sent": 0,
                "bytes_recv": 0,
                "packets_sent": 0,
                "packets_recv": 0,
            }
        return {
            "bytes_sent": int(counters.bytes_sent),
            "bytes_recv": int(counters.bytes_recv),
            "packets_sent": int(counters.packets_sent),
            "packets_recv": int(counters.packets_recv),
        }

    def _read_temperature(self) -> Optional[float]:
        try:
            raw = self._thermal_path.read_text(encoding="ascii").strip()
            if raw:
                celsius = float(raw) / 1000.0
                if -50.0 <= celsius <= 200.0:
                    return _round(celsius, 1)
        except (OSError, ValueError):
            pass
        try:
            sensors = psutil.sensors_temperatures()
        except Exception:
            # AttributeError on platforms without the API, OSError/psutil.Error
            # when the sensor is not accessible.
            sensors = None
        if sensors:
            for entries in sensors.values():
                for entry in entries or []:
                    current = getattr(entry, "current", None)
                    if current is None:
                        continue
                    try:
                        celsius = float(current)
                    except (TypeError, ValueError):
                        continue
                    if -50.0 <= celsius <= 200.0:
                        return _round(celsius, 1)
        return None

    def _collect_processes(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            for process in psutil.process_iter(
                attrs=["pid", "name", "cpu_percent", "memory_percent"]
            ):
                info = process.info
                try:
                    rows.append(
                        {
                            "pid": int(info.get("pid", 0)),
                            "name": str(info.get("name") or "?"),
                            "cpu_percent": _round(info.get("cpu_percent") or 0.0, 1),
                            "memory_percent": _round(
                                info.get("memory_percent") or 0.0, 1
                            ),
                        }
                    )
                except (TypeError, ValueError):
                    continue
        except Exception:
            logger.debug("Process list unavailable", exc_info=True)
        rows.sort(
            key=lambda item: (item["cpu_percent"], item["memory_percent"]),
            reverse=True,
        )
        return rows[: self._top_processes]
