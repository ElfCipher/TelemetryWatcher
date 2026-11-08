from __future__ import annotations

import itertools

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from telemetry.api import create_app
from telemetry.collector import MetricsCollector
from telemetry.config import Settings
from telemetry.storage import MetricsStorage


def test_ws_sends_latest_and_handles_commands(
    settings, storage, collector, seed, make_point
):
    app = create_app(
        settings,
        storage=storage,
        collector=collector,
        start_background=False,
    )
    with TestClient(app) as client:
        client.portal.call(storage.add, make_point(timestamp=1000.0))
        with client.websocket_connect("/ws/metrics") as websocket:
            message = websocket.receive_json()
            assert message["timestamp"] == 1000.0
            assert message["cpu"]["percent"] == 12.5

            websocket.send_text("set_interval:5")
            assert websocket.receive_json() == {
                "status": "ok",
                "interval_sec": 5.0,
            }
            assert collector.interval_sec == 5.0

            websocket.send_text("set_interval:0.1")
            error = websocket.receive_json()
            assert error["status"] == "error"
            assert "between" in error["error"]

            websocket.send_text("nonsense")
            error = websocket.receive_json()
            assert error["status"] == "error"
            assert "unknown command" in error["error"]

            websocket.send_text("ping")
            assert websocket.receive_json() == {"status": "ok", "pong": True}


def test_ws_streams_new_points(make_point):
    storage = MetricsStorage(maxlen=20)
    counter = itertools.count()

    def fake_collect():
        return make_point(timestamp=2000.0 + next(counter))

    collector = MetricsCollector(
        storage,
        interval_sec=0.05,
        min_interval_sec=0.01,
        collect_func=fake_collect,
    )
    settings = Settings(
        interval_sec=0.05, min_interval_sec=0.01, start_collector=True
    )
    app = create_app(settings, storage=storage, collector=collector)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/metrics") as websocket:
            first = websocket.receive_json()
            second = websocket.receive_json()
            assert second["timestamp"] > first["timestamp"]

            websocket.send_text("set_interval:0.1")
            response = None
            for _ in range(40):
                message = websocket.receive_json()
                if message.get("status") == "ok":
                    response = message
                    break
            assert response is not None
            assert response["interval_sec"] == 0.1
            assert collector.interval_sec == 0.1


def test_ws_auth(settings, storage, collector):
    settings.auth_token = "secret"
    app = create_app(
        settings,
        storage=storage,
        collector=collector,
        start_background=False,
    )
    with TestClient(app) as client:
        closed = False
        try:
            with client.websocket_connect("/ws/metrics") as websocket:
                websocket.receive_json()
        except WebSocketDisconnect:
            closed = True
        assert closed

        closed = False
        try:
            with client.websocket_connect("/ws/metrics?token=wrong") as websocket:
                websocket.receive_json()
        except WebSocketDisconnect:
            closed = True
        assert closed

        with client.websocket_connect("/ws/metrics?token=secret") as websocket:
            websocket.send_text("ping")
            assert websocket.receive_json() == {"status": "ok", "pong": True}
