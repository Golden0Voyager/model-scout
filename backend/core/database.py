"""SQLite persistence layer for health checks and scan history.

Writes are serialised in Python and each operation opens a short-lived connection.

A pooled connection was tried and reverted: it must be owned and closed by a single
event loop, which produced a check-then-act race (a concurrent reset made the accessor
return None) and hangs at interpreter exit, since aiosqlite's worker thread is
non-daemon and a connection stranded on a closed loop can never be closed.

WAL alone is not enough for concurrent writers: when two connections both promote a
read transaction to a write, SQLite returns SQLITE_BUSY immediately and ignores
busy_timeout. Hence the write gate below. Readers are still never blocked, which is
what WAL is genuinely for.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "model_scout.db")

_write_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


@asynccontextmanager
async def write_lock() -> AsyncIterator[None]:
    """Serialize write transactions against the database file.

    Rebound per event loop because an asyncio.Lock is not shareable across loops. In
    production there is exactly one loop; a second loop getting its own lock cannot
    corrupt anything, it just falls back to WAL plus SQLite's own busy handling.
    """
    global _write_lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _write_lock is None or _lock_loop is not loop:
        _write_lock = asyncio.Lock()
        _lock_loop = loop
    async with _write_lock:
        yield


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000")
        yield db


async def init_db() -> None:
    """Create tables if they don't exist, and put the database in WAL mode."""
    async with write_lock(), connect() as db:
        # journal_mode persists in the database file, so setting it once is enough.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS health_checks (
                model_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown',
                latency_ms INTEGER,
                error_message TEXT,
                last_checked TEXT,
                PRIMARY KEY (model_id, provider)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scan_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                models_checked INTEGER DEFAULT 0,
                models_online INTEGER DEFAULT 0,
                error TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_health_provider ON health_checks(provider)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS provider_settings (
                provider_key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.commit()


async def get_provider_prefs() -> dict[str, bool]:
    """Stored switches only; providers with no row fall back to their config default."""
    async with connect() as db, db.execute("SELECT provider_key, enabled FROM provider_settings") as cursor:
        rows = await cursor.fetchall()
    return {row[0]: bool(row[1]) for row in rows}


async def set_provider_pref(provider_key: str, enabled: bool) -> None:
    now = datetime.now(UTC).isoformat()
    async with write_lock(), connect() as db:
        await db.execute(
            """
            INSERT INTO provider_settings (provider_key, enabled, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(provider_key) DO UPDATE SET
                enabled=excluded.enabled,
                updated_at=excluded.updated_at
            """,
            (provider_key, int(enabled), now),
        )
        await db.commit()


async def upsert_health(check: dict[str, Any]) -> None:
    async with write_lock(), connect() as db:
        await db.execute("""
            INSERT INTO health_checks (model_id, provider, status, latency_ms, error_message, last_checked)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_id, provider) DO UPDATE SET
                status=excluded.status,
                latency_ms=excluded.latency_ms,
                error_message=excluded.error_message,
                last_checked=excluded.last_checked
        """, (
            check["model_id"],
            check["provider"],
            check.get("status", "unknown"),
            check.get("latency_ms"),
            check.get("error_message"),
            check.get("last_checked", datetime.now(UTC).isoformat()),
        ))
        await db.commit()


async def get_all_health() -> list[dict[str, Any]]:
    async with connect() as db, db.execute("SELECT * FROM health_checks") as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_health_for_provider(provider: str) -> list[dict[str, Any]]:
    async with connect() as db, db.execute(
        "SELECT * FROM health_checks WHERE provider = ?", (provider,)
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def log_scan_start() -> int | None:
    now = datetime.now(UTC).isoformat()
    async with write_lock(), connect() as db:
        cursor = await db.execute("INSERT INTO scan_log (started_at) VALUES (?)", (now,))
        await db.commit()
        return cursor.lastrowid


async def log_scan_finish(
    scan_id: int | None, models_checked: int, models_online: int, error: str | None = None
) -> None:
    if scan_id is None:
        return
    now = datetime.now(UTC).isoformat()
    async with write_lock(), connect() as db:
        await db.execute(
            """
            UPDATE scan_log
            SET finished_at = ?, models_checked = ?, models_online = ?, error = ?
            WHERE id = ?
            """,
            (now, models_checked, models_online, error, scan_id),
        )
        await db.commit()


async def get_last_scan_time() -> str | None:
    async with connect() as db, db.execute(
        "SELECT finished_at FROM scan_log WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_scan_stats() -> dict[str, Any]:
    async with connect() as db, db.execute(
        "SELECT COUNT(*), SUM(models_online) FROM scan_log WHERE finished_at IS NOT NULL"
    ) as cursor:
        row = await cursor.fetchone()
    total_scans, total_online_ever = (row[0], row[1]) if row else (0, 0)
    return {
        "total_scans": total_scans or 0,
        "total_online_ever": total_online_ever or 0,
    }
