"""Pydantic models for API request/response validation."""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime


class ModelInfo(BaseModel):
    id: str
    name: str
    provider: str
    provider_name: str = ""
    context_length: int = 0
    max_output_tokens: Optional[int] = None
    description: str = ""
    description_cn: str = ""
    capabilities: List[str] = Field(default_factory=list)
    pricing_input_per_1m: Optional[float] = None
    pricing_output_per_1m: Optional[float] = None
    pricing_currency: str = "CNY"
    pricing_note: str = ""
    is_free: bool = False


class HealthStatus(BaseModel):
    model_id: str
    provider: str
    status: str = "unknown"  # online | offline | unknown | error
    latency_ms: Optional[int] = None
    error_message: Optional[str] = None
    last_checked: Optional[str] = None


class ModelWithHealth(ModelInfo):
    health: HealthStatus = Field(default_factory=lambda: HealthStatus(model_id="", provider=""))


class ProviderSummary(BaseModel):
    key: str
    name: str
    model_count: int
    online_count: int
    avg_latency_ms: Optional[int] = None
    last_scan: Optional[str] = None


class DashboardResponse(BaseModel):
    models: List[ModelWithHealth]
    providers: List[ProviderSummary]
    total_models: int
    online_models: int
    avg_latency_ms: Optional[int] = None
    last_scan_time: Optional[str] = None
    is_scanning: bool = False


class ScanTriggerResponse(BaseModel):
    status: str
    message: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str = "2.0.0"
    uptime_seconds: float = 0.0
