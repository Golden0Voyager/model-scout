"""SQLite persistence layer for health checks and scan history.

Uses aiosqlite for async operations.
"""

import os
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "model_scout.db")


async def init_db() -> None:
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
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
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_health_provider ON health_checks(provider)
        """)
        await db.commit()


async def upsert_health(check: Dict[str, Any]) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
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
            check.get("last_checked", datetime.now(timezone.utc).isoformat()),
        ))
        await db.commit()


async def get_all_health() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM health_checks") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_health_for_provider(provider: str) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM health_checks WHERE provider = ?", (provider,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def log_scan_start() -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO scan_log (started_at) VALUES (?)", (now,)
        )
        await db.commit()
        return cursor.lastrowid


async def log_scan_finish(
    scan_id: int, models_checked: int, models_online: int, error: Optional[str] = None
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE scan_log
            SET finished_at = ?, models_checked = ?, models_online = ?, error = ?
            WHERE id = ?
            """,
            (now, models_checked, models_online, error, scan_id),
        )
        await db.commit()


async def get_last_scan_time() -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT finished_at FROM scan_log WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_scan_stats() -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(models_online) FROM scan_log WHERE finished_at IS NOT NULL"
        ) as cursor:
            row = await cursor.fetchone()
            total_scans = row[0] or 0
            total_online_ever = row[1] or 0
        return {
            "total_scans": total_scans,
            "total_online_ever": total_online_ever,
        }
