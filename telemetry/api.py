"""FastAPI application: REST endpoints, WebSocket stream and static frontend."""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import math
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from . import __version__
from .collector import MetricsCollector
from .config import Settings
from .models import (
    ConfigResponse,
    ConfigUpdate,
    HealthResponse,
    MetricPoint,
    ProcessInfo,
)
from .storage import MetricsStorage

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    "timestamp",
    "cpu_percent",
    "mem_percent",
    "temp_c",
    "disk_percent",
    "net_sent",
    "net_recv",
    "mem_used_mb",
    "mem_total_mb",
    "disk_used_mb",
    "disk_total_mb",
    "net_packets_sent",
    "net_packets_recv",
]


def _bearer_token(header_value: str) -> Optional[str]:
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _tokens_equal(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _validate_range(start: float, end: float, step: Optional[float] = None) -> None:
    if not math.isfinite(start) or not math.isfinite(end):
        raise HTTPException(status_code=422, detail="start and end must be finite numbers")
    if start > end:
        raise HTTPException(
            status_code=400, detail="start must be less than or equal to end"
        )
    if step is not None and (not math.isfinite(step) or step <= 0):
        raise HTTPException(status_code=422, detail="step must be a positive number")


def _number(value: Any, digits: int = 1) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return ""


def _timestamp_label(value: float) -> str:
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _csv_row(point: Dict[str, Any]) -> List[str]:
    cpu = point.get("cpu") or {}
    memory = point.get("memory") or {}
    disk = point.get("disk") or {}
    network = point.get("network") or {}
    mem_total = float(memory.get("total") or 0.0)
    mem_available = float(memory.get("available") or 0.0)
    return [
        f"{float(point.get('timestamp', 0.0)):.3f}",
        _number(cpu.get("percent")),
        _number(memory.get("percent")),
        _number(point.get("temperature_c")),
        _number(disk.get("percent")),
        str(int(network.get("bytes_sent") or 0)),
        str(int(network.get("bytes_recv") or 0)),
        _number(max(0.0, mem_total - mem_available)),
        _number(mem_total),
        _number(disk.get("used")),
        _number(disk.get("total")),
        str(int(network.get("packets_sent") or 0)),
        str(int(network.get("packets_recv") or 0)),
    ]


async def _handle_ws_message(
    message: str, collector: MetricsCollector
) -> Dict[str, Any]:
    text = message.strip()
    if text.startswith("set_interval:"):
        raw = text.partition(":")[2].strip()
        try:
            interval = await collector.set_interval(float(raw))
        except (TypeError, ValueError) as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok", "interval_sec": interval}
    if text == "ping":
        return {"status": "ok", "pong": True}
    return {
        "status": "error",
        "error": "unknown command; expected 'set_interval:<seconds>'",
    }


def create_app(
    settings: Optional[Settings] = None,
    *,
    storage: Optional[MetricsStorage] = None,
    collector: Optional[MetricsCollector] = None,
    start_background: Optional[bool] = None,
) -> FastAPI:
    """Create the FastAPI application.

    ``storage``/``collector``/``start_background`` exist for tests and for
    embedding the service in another process; production code can call
    ``create_app()`` with no arguments.
    """
    active_settings = settings or Settings.from_env()
    resolved_storage = storage
    if resolved_storage is None and collector is not None:
        resolved_storage = collector.storage

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_storage = (
            resolved_storage
            if resolved_storage is not None
            else MetricsStorage(
                maxlen=active_settings.buffer_size,
                sqlite_path=active_settings.sqlite_path,
                sqlite_flush_sec=active_settings.sqlite_flush_sec,
            )
        )
        await active_storage.open()
        active_collector = collector or MetricsCollector(
            active_storage,
            interval_sec=active_settings.interval_sec,
            top_processes=active_settings.top_processes,
            min_interval_sec=active_settings.min_interval_sec,
            max_interval_sec=active_settings.max_interval_sec,
        )
        app.state.storage = active_storage
        app.state.collector = active_collector
        should_start = (
            active_settings.start_collector
            if start_background is None
            else bool(start_background)
        )
        started = False
        try:
            if should_start:
                started = True
                await active_collector.start()
            yield
        finally:
            if started:
                await active_collector.stop()
            await active_storage.close()

    app = FastAPI(
        title="Telemetry",
        version=__version__,
        description="Lightweight system telemetry for Linux",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = active_settings

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if active_settings.auth_token and request.url.path.startswith("/api/"):
            supplied = _bearer_token(request.headers.get("authorization", ""))
            if supplied is None or not _tokens_equal(
                supplied, active_settings.auth_token
            ):
                return JSONResponse(
                    {"detail": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    router = APIRouter(prefix="/api/v1")

    def get_storage(request: Request) -> MetricsStorage:
        return request.app.state.storage

    def get_collector(request: Request) -> MetricsCollector:
        return request.app.state.collector

    @router.get("/metrics", response_model=MetricPoint)
    async def current_metrics(request: Request) -> MetricPoint:
        point = await get_storage(request).latest()
        if point is None:
            raise HTTPException(status_code=503, detail="No metrics collected yet")
        return point

    @router.get("/metrics/history", response_model=List[MetricPoint])
    async def metrics_history(
        request: Request,
        start: float = Query(..., description="Unix timestamp, inclusive"),
        end: float = Query(..., description="Unix timestamp, inclusive"),
        step: float = Query(1.0, ge=1.0, description="Thinning step in seconds"),
    ) -> List[Dict[str, Any]]:
        _validate_range(start, end, step)
        return await get_storage(request).history(start, end, step)

    @router.get("/processes", response_model=List[ProcessInfo])
    async def processes(
        request: Request,
        limit: int = Query(5, ge=1, le=50),
        sort: Literal["cpu", "memory"] = Query("cpu"),
    ) -> List[Dict[str, Any]]:
        point = await get_storage(request).latest()
        if point is None:
            raise HTTPException(status_code=503, detail="No metrics collected yet")
        items = list(point.get("top_processes") or [])
        field = "cpu_percent" if sort == "cpu" else "memory_percent"
        items.sort(key=lambda item: float(item.get(field) or 0.0), reverse=True)
        return items[:limit]

    @router.get("/export")
    async def export(
        request: Request,
        start: float = Query(...),
        end: float = Query(...),
        format: Literal["csv", "json"] = Query("csv"),
    ) -> Response:
        _validate_range(start, end)
        points = await get_storage(request).history(start, end, 1.0)
        filename = (
            f"telemetry_{_timestamp_label(start)}_{_timestamp_label(end)}.{format}"
        )
        headers = {"Content-Disposition": f"attachment; filename={filename}"}
        if format == "json":
            return JSONResponse(content=points, headers=headers)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for point in points:
            writer.writerow(_csv_row(point))
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers=headers,
        )

    @router.post("/config", response_model=ConfigResponse)
    async def set_config(
        payload: ConfigUpdate, request: Request
    ) -> ConfigResponse:
        try:
            interval = await get_collector(request).set_interval(payload.interval_sec)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ConfigResponse(status="ok", interval_sec=interval)

    @router.get("/config")
    async def get_config(request: Request) -> Dict[str, Any]:
        collector_obj = get_collector(request)
        storage_obj = get_storage(request)
        return {
            "interval_sec": collector_obj.interval_sec,
            "buffer_size": storage_obj.maxlen,
            "sqlite_enabled": storage_obj.sqlite_enabled,
            "version": __version__,
        }

    app.include_router(router)

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        storage_obj = request.app.state.storage
        collector_obj = request.app.state.collector
        return HealthResponse(
            status="ok",
            interval_sec=collector_obj.interval_sec,
            buffered=len(storage_obj),
            version=__version__,
        )

    @app.websocket("/ws/metrics")
    async def websocket_metrics(websocket: WebSocket) -> None:
        if active_settings.auth_token:
            supplied = _bearer_token(websocket.headers.get("authorization", ""))
            if supplied is None:
                supplied = websocket.query_params.get("token")
            if supplied is None or not _tokens_equal(
                supplied, active_settings.auth_token
            ):
                await websocket.close(code=4401)
                return

        await websocket.accept()
        storage_obj: MetricsStorage = websocket.app.state.storage
        collector_obj: MetricsCollector = websocket.app.state.collector
        send_lock = asyncio.Lock()
        queue = collector_obj.broadcaster.register()

        async def send_json(payload: Dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        last_timestamp: Optional[float] = None
        try:
            latest = await storage_obj.latest()
            if latest is not None:
                await send_json(latest)
                last_timestamp = latest.get("timestamp")

            async def sender() -> None:
                nonlocal last_timestamp
                while True:
                    point = await queue.get()
                    if (
                        last_timestamp is not None
                        and point.get("timestamp") == last_timestamp
                    ):
                        continue
                    await send_json(point)
                    last_timestamp = point.get("timestamp")

            async def receiver() -> None:
                while True:
                    message = await websocket.receive_text()
                    response = await _handle_ws_message(message, collector_obj)
                    await send_json(response)

            tasks = [
                asyncio.create_task(sender(), name="telemetry-ws-sender"),
                asyncio.create_task(receiver(), name="telemetry-ws-receiver"),
            ]
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                # Cancel the other task but do not await it here: when the
                # client disconnects, Starlette/TestClient may cancel this
                # handler immediately, and any extra await opens a race where
                # the cancellation escapes as a noisy CancelledError.
                for task in pending:
                    task.cancel()
                for task in done:
                    if task.cancelled():
                        continue
                    exception = task.exception()
                    if exception is not None and not isinstance(
                        exception, (WebSocketDisconnect, asyncio.CancelledError)
                    ):
                        logger.debug("WebSocket closed: %s", exception)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
        finally:
            collector_obj.broadcaster.unregister(queue)

    frontend_dir = active_settings.frontend_dir
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    else:

        @app.get("/")
        async def frontend_missing() -> JSONResponse:
            return JSONResponse(
                {"detail": "Frontend files are not installed", "path": str(frontend_dir)},
                status_code=404,
            )

    return app
