"""SQLite persistence layer for health checks and scan history.

One pooled connection is shared by every caller. Opening a fresh connection per call
meant each write raced the others in DELETE journal mode, so a scan persisting
hundreds of rows while the dashboard polled could raise "database is locked". A
single connection (aiosqlite serialises its statements on one worker thread) plus
WAL removes that whole class of failure: readers no longer block on the writer.
"""

import os
from datetime import UTC, datetime
from typing import Any

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "model_scout.db")

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    """The pooled connection, opened on first use and reused thereafter."""
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        # WAL so a dashboard poll is never blocked by a scan mid-write; the timeout
        # covers the short windows where two writes genuinely overlap.
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA busy_timeout=5000")
        await _db.commit()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def init_db() -> None:
    """Create tables if they don't exist."""
    db = await get_db()
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
    await db.execute("CREATE INDEX IF NOT EXISTS idx_health_provider ON health_checks(provider)")
    await db.commit()


async def upsert_health(check: dict[str, Any]) -> None:
    db = await get_db()
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
    db = await get_db()
    async with db.execute("SELECT * FROM health_checks") as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_health_for_provider(provider: str) -> list[dict[str, Any]]:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM health_checks WHERE provider = ?", (provider,)
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def log_scan_start() -> int | None:
    now = datetime.now(UTC).isoformat()
    db = await get_db()
    cursor = await db.execute("INSERT INTO scan_log (started_at) VALUES (?)", (now,))
    await db.commit()
    return cursor.lastrowid


async def log_scan_finish(
    scan_id: int | None, models_checked: int, models_online: int, error: str | None = None
) -> None:
    if scan_id is None:
        return
    now = datetime.now(UTC).isoformat()
    db = await get_db()
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
    db = await get_db()
    async with db.execute(
        "SELECT finished_at FROM scan_log WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_scan_stats() -> dict[str, Any]:
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*), SUM(models_online) FROM scan_log WHERE finished_at IS NOT NULL"
    ) as cursor:
        row = await cursor.fetchone()
    total_scans, total_online_ever = (row[0], row[1]) if row else (0, 0)
    return {
        "total_scans": total_scans or 0,
        "total_online_ever": total_online_ever or 0,
    }
