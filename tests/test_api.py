from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient

from telemetry.api import CSV_COLUMNS, create_app
from telemetry.config import Settings
from telemetry.storage import MetricsStorage


def test_current_metrics_empty(client):
    response = client.get("/api/v1/metrics")
    assert response.status_code == 503
    assert response.json()["detail"] == "No metrics collected yet"


def test_current_metrics(seeded_client):
    response = seeded_client.get("/api/v1/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["timestamp"] == 1004.0
    assert payload["cpu"]["percent"] == 12.5
    assert payload["temperature_c"] == 48.2
    assert payload["memory"]["total"] == 3936.0
    assert payload["network"]["bytes_recv"] == 1234567
    assert len(payload["top_processes"]) == 2


def test_history_filter_and_thinning(seeded_client):
    response = seeded_client.get(
        "/api/v1/metrics/history", params={"start": 1001, "end": 1003}
    )
    assert response.status_code == 200
    assert [point["timestamp"] for point in response.json()] == [
        1001.0,
        1002.0,
        1003.0,
    ]

    thinned = seeded_client.get(
        "/api/v1/metrics/history",
        params={"start": 1000, "end": 1004, "step": 2},
    )
    assert [point["timestamp"] for point in thinned.json()] == [1000.0, 1002.0, 1004.0]

    everything = seeded_client.get(
        "/api/v1/metrics/history", params={"start": 1000, "end": 1004}
    )
    assert len(everything.json()) == 5


def test_history_validation(client):
    assert client.get("/api/v1/metrics/history").status_code == 422
    assert (
        client.get(
            "/api/v1/metrics/history", params={"start": 10, "end": 5}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/v1/metrics/history",
            params={"start": 0, "end": 10, "step": 0},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/metrics/history",
            params={"start": "nan", "end": 10},
        ).status_code
        in (400, 422)
    )


def test_processes_empty(client):
    assert client.get("/api/v1/processes").status_code == 503


def test_processes_sorting_and_limit(client, seed, make_point):
    seed(
        [
            make_point(
                timestamp=1000.0,
                processes=[
                    {
                        "pid": 1,
                        "name": "alpha",
                        "cpu_percent": 5.0,
                        "memory_percent": 50.0,
                    },
                    {
                        "pid": 2,
                        "name": "beta",
                        "cpu_percent": 50.0,
                        "memory_percent": 5.0,
                    },
                    {
                        "pid": 3,
                        "name": "gamma",
                        "cpu_percent": 20.0,
                        "memory_percent": 30.0,
                    },
                ],
            )
        ]
    )
    by_cpu = client.get("/api/v1/processes", params={"sort": "cpu"}).json()
    assert [process["pid"] for process in by_cpu] == [2, 3, 1]

    by_memory = client.get("/api/v1/processes", params={"sort": "memory"}).json()
    assert [process["pid"] for process in by_memory] == [1, 3, 2]

    limited = client.get("/api/v1/processes", params={"limit": 2}).json()
    assert len(limited) == 2

    assert (
        client.get("/api/v1/processes", params={"sort": "disk"}).status_code == 422
    )
    assert client.get("/api/v1/processes", params={"limit": 0}).status_code == 422


def test_config_get_and_update(client, collector):
    initial = client.get("/api/v1/config")
    assert initial.status_code == 200
    assert initial.json()["interval_sec"] == 2.0
    assert initial.json()["buffer_size"] == 50
    assert initial.json()["sqlite_enabled"] is False

    response = client.post("/api/v1/config", json={"interval_sec": 5})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "interval_sec": 5.0}
    assert collector.interval_sec == 5.0
    assert client.get("/api/v1/config").json()["interval_sec"] == 5.0

    for value in (0, -1, 0.1, 10000, "not-a-number"):
        assert (
            client.post("/api/v1/config", json={"interval_sec": value}).status_code
            == 422
        )
    assert client.post("/api/v1/config", json={}).status_code == 422


def test_export_csv(client, seed, make_point):
    seed(
        [
            make_point(timestamp=1000.0 + index, temperature_c=48.2)
            for index in range(5)
        ]
        + [make_point(timestamp=1010.0, temperature_c=None)]
    )
    response = client.get(
        "/api/v1/export", params={"start": 1000, "end": 1010}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        response.headers["content-disposition"]
        == "attachment; filename=telemetry_1000_1010.csv"
    )

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == CSV_COLUMNS
    assert len(rows) == 7
    assert rows[1][0] == "1000.000"
    assert rows[1][1] == "12.5"
    assert rows[1][2] == "46.7"
    assert rows[1][3] == "48.2"
    assert rows[1][4] == "40.5"
    assert rows[1][5] == "987654"
    assert rows[1][6] == "1234567"
    assert rows[-1][3] == ""


def test_export_json(client, seed, make_point):
    seed([make_point(timestamp=1000.0 + index) for index in range(3)])
    response = client.get(
        "/api/v1/export",
        params={"start": 1000, "end": 1002, "format": "json"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert (
        response.headers["content-disposition"]
        == "attachment; filename=telemetry_1000_1002.json"
    )
    payload = response.json()
    assert len(payload) == 3
    assert [point["timestamp"] for point in payload] == [1000.0, 1001.0, 1002.0]


def test_export_empty(client):
    response = client.get(
        "/api/v1/export", params={"start": 1000, "end": 2000}
    )
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows == [CSV_COLUMNS]


def test_export_validation(client):
    assert (
        client.get("/api/v1/export", params={"start": 10, "end": 5}).status_code
        == 400
    )
    assert (
        client.get(
            "/api/v1/export",
            params={"start": 0, "end": 10, "format": "xml"},
        ).status_code
        == 422
    )


def test_export_filename_has_no_scientific_notation(client):
    response = client.get(
        "/api/v1/export",
        params={"start": 1794083388.1786914, "end": 1794083448.1786914},
    )
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "e+" not in disposition
    assert "telemetry_1794083388.179_1794083448.179.csv" in disposition


def test_static_frontend(client):
    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "Телеметрия" in index.text

    assert client.get("/style.css").status_code == 200
    assert client.get("/app.js").status_code == 200
    assert client.get("/vendor/chart.umd.min.js").status_code == 200


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["interval_sec"] == 2.0
    assert payload["buffered"] == 0
    assert payload["version"] == "1.0.0"


def test_auth_token_required(settings, storage, collector):
    settings.auth_token = "topsecret"
    app = create_app(
        settings,
        storage=storage,
        collector=collector,
        start_background=False,
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/metrics").status_code == 401
        assert (
            client.get(
                "/api/v1/metrics",
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/metrics",
                headers={"Authorization": "Bearer topsecret"},
            ).status_code
            == 503
        )
        assert client.get("/health").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/api/v1/config").status_code == 401
        assert (
            client.post(
                "/api/v1/config",
                json={"interval_sec": 2},
                headers={"Authorization": "Bearer topsecret"},
            ).status_code
            == 200
        )


def test_default_app_builds_state():
    settings = Settings(start_collector=False, buffer_size=10)
    app = create_app(settings, start_background=False)
    with TestClient(app) as client:
        assert isinstance(app.state.storage, MetricsStorage)
        assert app.state.storage.maxlen == 10
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/metrics").status_code == 503
