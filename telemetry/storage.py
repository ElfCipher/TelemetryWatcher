"""In-memory ring buffer with optional SQLite persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

Point = Dict[str, Any]


class SqliteStore:
    """Tiny JSON-per-row SQLite store.

    A fresh connection is opened for every operation so that calls can safely
    run inside ``asyncio.to_thread`` worker threads.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _init_db(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metrics ("
                "ts REAL PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.commit()

    def save(self, points: Iterable[Point], keep: int) -> int:
        rows = [
            (float(point["timestamp"]), json.dumps(point, separators=(",", ":")))
            for point in points
        ]
        with closing(self._connect()) as connection:
            if rows:
                connection.executemany(
                    "INSERT OR REPLACE INTO metrics (ts, payload) VALUES (?, ?)",
                    rows,
                )
                # Keep the on-disk history bounded by the same size as the ring buffer.
                connection.execute(
                    "DELETE FROM metrics WHERE ts NOT IN "
                    "(SELECT ts FROM metrics ORDER BY ts DESC LIMIT ?)",
                    (int(keep),),
                )
            else:
                # An explicitly emptied buffer must also clear persisted history.
                connection.execute("DELETE FROM metrics")
            connection.commit()
        return len(rows)

    def load_recent(self, limit: int) -> List[Point]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload FROM metrics ORDER BY ts DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]


class MetricsStorage:
    """Ring buffer of metric points guarded by an :class:`asyncio.Lock`."""

    def __init__(
        self,
        maxlen: int = 3600,
        sqlite_path: Optional[Path] = None,
        sqlite_flush_sec: float = 30.0,
    ) -> None:
        if int(maxlen) < 1:
            raise ValueError("maxlen must be >= 1")
        self._maxlen = int(maxlen)
        self._points: Deque[Point] = deque(maxlen=self._maxlen)
        self._lock = asyncio.Lock()
        self._sqlite = SqliteStore(Path(sqlite_path)) if sqlite_path else None
        self._flush_sec = float(sqlite_flush_sec)
        self._flush_task: Optional[asyncio.Task] = None
        self._opened = False
        self._dirty = False

    @property
    def maxlen(self) -> int:
        return self._maxlen

    @property
    def sqlite_enabled(self) -> bool:
        return self._sqlite is not None

    def __len__(self) -> int:
        return len(self._points)

    def preload(self, points: Iterable[Point]) -> None:
        """Synchronously seed the buffer before the service starts."""
        ordered = sorted(points, key=lambda item: float(item["timestamp"]))
        for point in ordered[-self._maxlen :]:
            self._points.append(point)

    async def open(self) -> None:
        """Load persisted points (if any) and start the flush loop."""
        if self._opened:
            return
        if self._sqlite is not None:
            if not self._points:
                loaded = await asyncio.to_thread(
                    self._sqlite.load_recent, self._maxlen
                )
                if loaded:
                    async with self._lock:
                        self._points.extend(loaded)
            self._flush_task = asyncio.create_task(
                self._flush_loop(), name="telemetry-sqlite-flush"
            )
        self._opened = True

    async def close(self) -> None:
        """Stop background flushing and persist pending points."""
        task = self._flush_task
        self._flush_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self.flush()
        finally:
            self._opened = False

    async def add(self, point: Point) -> None:
        async with self._lock:
            self._points.append(point)
            self._dirty = True

    async def latest(self) -> Optional[Point]:
        async with self._lock:
            if not self._points:
                return None
            return dict(self._points[-1])

    async def snapshot(self) -> List[Point]:
        async with self._lock:
            return list(self._points)

    async def history(
        self, start: float, end: float, step: float = 1.0
    ) -> List[Point]:
        """Return points in ``[start, end]``, optionally thinned by ``step`` seconds.

        ``step <= 1`` means "no thinning", matching the protocol default.
        """
        points = [p for p in await self.snapshot() if start <= p["timestamp"] <= end]
        points.sort(key=lambda item: item["timestamp"])
        if step <= 1.0 or len(points) <= 1:
            return points
        result: List[Point] = []
        last_timestamp: Optional[float] = None
        for point in points:
            timestamp = float(point["timestamp"])
            if last_timestamp is None or timestamp - last_timestamp >= step - 1e-9:
                result.append(point)
                last_timestamp = timestamp
        return result

    async def clear(self) -> None:
        async with self._lock:
            self._points.clear()
            self._dirty = True

    async def flush(self) -> int:
        """Persist the current buffer. Returns the number of written points."""
        if self._sqlite is None:
            return 0
        async with self._lock:
            if not self._dirty:
                return 0
            points = list(self._points)
            self._dirty = False
        try:
            await asyncio.to_thread(self._sqlite.save, points, self._maxlen)
        except Exception:
            async with self._lock:
                self._dirty = True
            raise
        return len(points)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_sec)
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to flush metrics to SQLite")
