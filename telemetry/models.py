"""Pydantic models describing the telemetry protocol."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class CpuMetrics(BaseModel):
    percent: float = Field(0.0, description="Total CPU usage, percent")
    per_cpu: List[float] = Field(default_factory=list, description="Per-core usage, percent")
    load_avg: List[float] = Field(
        default_factory=list, description="1/5/15 minute load averages"
    )


class MemoryMetrics(BaseModel):
    total: float = Field(0.0, description="Total memory, MB")
    available: float = Field(0.0, description="Available memory, MB")
    percent: float = Field(0.0, description="Used memory, percent")


class DiskMetrics(BaseModel):
    total: float = Field(0.0, description="Total disk space, MB")
    used: float = Field(0.0, description="Used disk space, MB")
    percent: float = Field(0.0, description="Used disk space, percent")
    read_bytes: int = Field(0, description="Cumulative bytes read")
    write_bytes: int = Field(0, description="Cumulative bytes written")


class NetworkMetrics(BaseModel):
    bytes_sent: int = Field(0, description="Cumulative bytes sent")
    bytes_recv: int = Field(0, description="Cumulative bytes received")
    packets_sent: int = Field(0, description="Cumulative packets sent")
    packets_recv: int = Field(0, description="Cumulative packets received")


class ProcessInfo(BaseModel):
    pid: int
    name: str
    cpu_percent: float = Field(0.0, description="CPU usage relative to one core")
    memory_percent: float = Field(0.0, description="Resident memory, percent")


class MetricPoint(BaseModel):
    timestamp: float = Field(..., description="Unix timestamp in seconds")
    cpu: CpuMetrics = Field(default_factory=CpuMetrics)
    temperature_c: Optional[float] = Field(None, description="SoC temperature, Celsius")
    memory: MemoryMetrics = Field(default_factory=MemoryMetrics)
    disk: DiskMetrics = Field(default_factory=DiskMetrics)
    network: NetworkMetrics = Field(default_factory=NetworkMetrics)
    top_processes: List[ProcessInfo] = Field(default_factory=list)


class ConfigUpdate(BaseModel):
    interval_sec: float = Field(..., gt=0, description="Collection interval in seconds")


class ConfigResponse(BaseModel):
    status: str = "ok"
    interval_sec: float


class HealthResponse(BaseModel):
    status: str
    interval_sec: float
    buffered: int
    version: str
