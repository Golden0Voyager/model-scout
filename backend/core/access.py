"""Access controls for the scan endpoints.

The backend binds to loopback, but a browser sends a cross-origin POST to
127.0.0.1 without a preflight, so any page open in the user's browser can trigger
paid health probes. Browsers always attach `Origin` to such a request, which lets an
allowlist stop that whole class of abuse without shipping a shared secret to the
frontend — a secret readable by the page would be readable by page scripts too.

`Origin: null` is rejected on purpose: sandboxed iframes and file:// documents send
it, and it is the classic way to slip past a same-origin assumption.

A request with no `Origin` is treated as local. curl and scripts on this machine
already have every API key in backend/.env, so requiring a token from them would
protect nothing. Binding the server beyond loopback is the case that would need real
authentication, and it is out of scope here.
"""

import time
from collections import deque

from fastapi import HTTPException, Request

# Single source of truth, also consumed by the CORS middleware in app.py.
ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)

# Per-model probes each cost one real request and bypass the full-scan slot, so they
# need their own ceiling. Generous enough for clicking around the dashboard.
PROBE_MAX_CALLS = 30
PROBE_WINDOW_SECONDS = 60.0


def is_trusted_origin(origin: str | None) -> bool:
    return origin is None or origin in ALLOWED_ORIGINS


async def require_trusted_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not is_trusted_origin(origin):
        raise HTTPException(
            status_code=403,
            detail=f"Cross-origin write requests are not allowed (origin={origin})",
        )


class SlidingWindowLimiter:
    """Rate limit over a rolling window, independent of any scan-slot state."""

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._hits: deque[float] = deque()

    def acquire(self) -> float | None:
        """Take a slot. Returns None when allowed, else seconds until one frees."""
        now = time.monotonic()
        while self._hits and now - self._hits[0] >= self._window:
            self._hits.popleft()
        if len(self._hits) < self._max_calls:
            self._hits.append(now)
            return None
        return self._window - (now - self._hits[0])

    def reset(self) -> None:
        self._hits.clear()


probe_limiter = SlidingWindowLimiter(PROBE_MAX_CALLS, PROBE_WINDOW_SECONDS)
