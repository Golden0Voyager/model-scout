"""USD/CNY reference rate for the dashboard's price comparison.

Fetched server-side rather than by the browser so the read path stays free of outbound
calls — GET /api/models only ever serves the cached value — and so a source outage
degrades to the fallback constant instead of blanking out prices.

Two sources are tried in order because a free key-less FX endpoint genuinely does go
away: during this work exchangerate.host already returned {"success": false}.
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from core.config import CNY_PER_USD_BOUNDS, FALLBACK_CNY_PER_USD

SOURCES: tuple[str, ...] = (
    "https://open.er-api.com/v6/latest/USD",
    "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY",
)
REQUEST_TIMEOUT_SECONDS = 10.0


class FxRate:
    """Holds the last good USD/CNY rate, falling back to the bundled constant."""

    def __init__(self, proxy: str | None = None, client: httpx.AsyncClient | None = None) -> None:
        # Both sources are overseas hosts, so this follows the same proxy decision the
        # providers marked network="proxy" make. trust_env=False keeps that deliberate.
        self._client = client or httpx.AsyncClient(
            proxy=proxy,
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            trust_env=False,
        )
        self.cny_per_usd: float = FALLBACK_CNY_PER_USD
        self.source: str | None = None
        self.fetched_at: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def refresh(self) -> dict[str, Any]:
        """Try each source in order. Never raises: a bad day leaves the last good rate."""
        failures: list[str] = []

        for url in SOURCES:
            try:
                response = await self._client.get(url)
                if response.status_code != 200:
                    failures.append(f"{urlparse(url).netloc}: HTTP {response.status_code}")
                    continue
                rate = float(response.json()["rates"]["CNY"])
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                failures.append(f"{urlparse(url).netloc}: {type(exc).__name__}")
                continue

            if not CNY_PER_USD_BOUNDS[0] < rate < CNY_PER_USD_BOUNDS[1]:
                failures.append(f"{urlparse(url).netloc}: out-of-range {rate}")
                continue

            self.cny_per_usd = rate
            self.source = urlparse(url).netloc
            self.fetched_at = datetime.now(UTC).isoformat()
            return {"status": "updated", "cny_per_usd": rate, "source": self.source}

        return {
            "status": "fallback",
            "cny_per_usd": self.cny_per_usd,
            "source": self.source,
            "failures": failures,
        }
