from __future__ import annotations

import asyncio

import pytest

from telemetry.storage import MetricsStorage, SqliteStore


async def test_add_latest_and_snapshot(make_point):
    storage = MetricsStorage(maxlen=10)
    await storage.open()
    await storage.add(make_point(timestamp=1.0))
    await storage.add(make_point(timestamp=2.0))

    latest = await storage.latest()
    assert latest is not None
    assert latest["timestamp"] == 2.0
    assert [point["timestamp"] for point in await storage.snapshot()] == [1.0, 2.0]
    assert len(storage) == 2
    await storage.close()


async def test_empty_storage(make_point):
    storage = MetricsStorage(maxlen=5)
    await storage.open()
    assert await storage.latest() is None
    assert await storage.snapshot() == []
    assert await storage.history(0, 100) == []
    await storage.close()


async def test_ring_buffer_drops_old_points(make_point):
    storage = MetricsStorage(maxlen=3)
    await storage.open()
    for index in range(5):
        await storage.add(make_point(timestamp=float(index)))
    assert [point["timestamp"] for point in await storage.snapshot()] == [2.0, 3.0, 4.0]
    await storage.close()


async def test_history_range_and_thinning(make_point):
    storage = MetricsStorage(maxlen=20)
    await storage.open()
    # Insert out of order on purpose.
    for index in (4, 0, 2, 1, 3, 6, 5):
        await storage.add(make_point(timestamp=float(index)))

    all_points = await storage.history(0, 6)
    assert [point["timestamp"] for point in all_points] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

    bounded = await storage.history(1, 4)
    assert [point["timestamp"] for point in bounded] == [1.0, 2.0, 3.0, 4.0]

    stepped = await storage.history(0, 6, step=2)
    assert [point["timestamp"] for point in stepped] == [0.0, 2.0, 4.0, 6.0]

    stepped_5 = await storage.history(0, 6, step=5)
    assert [point["timestamp"] for point in stepped_5] == [0.0, 5.0]

    await storage.close()


async def test_preload_keeps_newest(make_point):
    storage = MetricsStorage(maxlen=3)
    storage.preload(
        [make_point(timestamp=float(index)) for index in (4, 1, 3, 0, 2)]
    )
    assert [point["timestamp"] for point in await storage.snapshot()] == [2.0, 3.0, 4.0]


async def test_concurrent_adds(make_point):
    storage = MetricsStorage(maxlen=200)
    await storage.open()
    await asyncio.gather(
        *(storage.add(make_point(timestamp=float(index))) for index in range(100))
    )
    assert len(await storage.snapshot()) == 100
    await storage.close()


async def test_sqlite_roundtrip(tmp_path, make_point):
    database = tmp_path / "metrics.db"
    first = MetricsStorage(maxlen=5, sqlite_path=database, sqlite_flush_sec=60)
    await first.open()
    for index in range(7):
        await first.add(make_point(timestamp=1000.0 + index))
    assert await first.flush() == 5
    await first.close()

    second = MetricsStorage(maxlen=5, sqlite_path=database, sqlite_flush_sec=60)
    await second.open()
    snapshot = await second.snapshot()
    assert [point["timestamp"] for point in snapshot] == [
        1002.0,
        1003.0,
        1004.0,
        1005.0,
        1006.0,
    ]
    await second.close()

    stored = SqliteStore(database).load_recent(10)
    assert len(stored) == 5


async def test_sqlite_background_flush(tmp_path, make_point):
    database = tmp_path / "metrics.db"
    storage = MetricsStorage(maxlen=10, sqlite_path=database, sqlite_flush_sec=0.02)
    await storage.open()
    await storage.add(make_point(timestamp=42.0))
    await asyncio.sleep(0.1)
    stored = SqliteStore(database).load_recent(10)
    assert len(stored) == 1
    assert stored[0]["timestamp"] == 42.0
    await storage.close()


async def test_flush_is_noop_without_sqlite(make_point):
    storage = MetricsStorage(maxlen=10)
    await storage.open()
    await storage.add(make_point())
    assert await storage.flush() == 0
    assert storage.sqlite_enabled is False
    await storage.close()


async def test_clear(make_point):
    storage = MetricsStorage(maxlen=10)
    await storage.open()
    await storage.add(make_point())
    await storage.clear()
    assert await storage.snapshot() == []
    await storage.close()


async def test_clear_persists_to_sqlite(tmp_path, make_point):
    database = tmp_path / "metrics.db"
    first = MetricsStorage(maxlen=10, sqlite_path=database, sqlite_flush_sec=60)
    await first.open()
    await first.add(make_point(timestamp=1.0))
    await first.flush()
    await first.clear()
    assert await first.flush() == 0
    await first.close()

    second = MetricsStorage(maxlen=10, sqlite_path=database, sqlite_flush_sec=60)
    await second.open()
    assert await second.snapshot() == []
    await second.close()


def test_invalid_maxlen():
    with pytest.raises(ValueError):
        MetricsStorage(maxlen=0)
