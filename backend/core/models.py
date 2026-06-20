"""Pydantic models for API request/response validation."""

from pydantic import BaseModel, Field


class ModelInfo(BaseModel):
    id: str
    name: str
    provider: str
    provider_name: str = ""
    context_length: int = 0
    max_output_tokens: int | None = None
    description: str = ""
    description_cn: str = ""
    capabilities: list[str] = Field(default_factory=list)
    pricing_input_per_1m: float | None = None
    pricing_output_per_1m: float | None = None
    pricing_currency: str = "CNY"
    pricing_note: str = ""
    is_free: bool = False


class HealthStatus(BaseModel):
    model_id: str
    provider: str
    status: str = "unknown"  # online | offline | unknown | error
    latency_ms: int | None = None
    error_message: str | None = None
    last_checked: str | None = None


class ModelWithHealth(ModelInfo):
    health: HealthStatus = Field(default_factory=lambda: HealthStatus(model_id="", provider=""))


class ProviderSummary(BaseModel):
    key: str
    name: str
    model_count: int
    online_count: int
    avg_latency_ms: int | None = None
    last_scan: str | None = None


class DashboardResponse(BaseModel):
    models: list[ModelWithHealth]
    providers: list[ProviderSummary]
    total_models: int
    online_models: int
    avg_latency_ms: int | None = None
    last_scan_time: str | None = None
    is_scanning: bool = False


class ScanTriggerResponse(BaseModel):
    status: str
    message: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str = "2.0.0"
    uptime_seconds: float = 0.0
