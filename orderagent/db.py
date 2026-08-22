"""Order history and job audit trail (FR-6 / NFR-1).

Every job transition is recorded, whether or not money moved: the audit
table is the answer to "did it click pay or not" when anything is ever in
doubt, so writes here happen BEFORE the action they describe as well as
after its outcome is known.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import threading
import time
from typing import Any, Optional

from . import config

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    job_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    chain TEXT NOT NULL,
    store_key TEXT,
    store_name TEXT,
    items_json TEXT,
    pickup TEXT,
    total_yen INTEGER,
    status TEXT NOT NULL,
    order_number TEXT,
    payment_executed INTEGER NOT NULL DEFAULT 0,
    device_id TEXT,
    snapshot_json TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    at REAL NOT NULL,
    event TEXT NOT NULL,
    detail_json TEXT
);
"""


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(_SCHEMA)
    # A database created before device_id existed: CREATE IF NOT EXISTS
    # leaves it alone, so the column is added here. Idempotent by probing.
    for column in ("device_id", "snapshot_json"):
        try:
            conn.execute(f"SELECT {column} FROM orders LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT")
    return conn


def record(job_id: str, event: str, detail: Optional[dict[str, Any]] = None) -> None:
    # closing() matters: sqlite3's own context manager is a transaction,
    # not a close, and an unclosed connection on Windows is a file lock
    # that outlives the call.
    with _lock, closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO audit (job_id, at, event, detail_json) VALUES (?, ?, ?, ?)",
                     (job_id, time.time(), event, json.dumps(detail or {}, ensure_ascii=False)))


def upsert_order(job: dict[str, Any]) -> None:
    with _lock, closing(_connect()) as conn, conn:
        conn.execute(
            """INSERT INTO orders (job_id, created_at, chain, store_key, store_name,
                                   items_json, pickup, total_yen, status, order_number,
                                   payment_executed, device_id, snapshot_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   store_key=excluded.store_key, store_name=excluded.store_name,
                   items_json=excluded.items_json, pickup=excluded.pickup,
                   total_yen=excluded.total_yen, status=excluded.status,
                   order_number=excluded.order_number,
                   payment_executed=excluded.payment_executed,
                   device_id=excluded.device_id,
                   snapshot_json=excluded.snapshot_json""",
            (job["job_id"], job["created_at"], job["chain"], job.get("store_key"),
             job.get("store_name"), json.dumps(job.get("items", []), ensure_ascii=False),
             job.get("pickup"), job.get("total_yen"), job["status"],
             job.get("order_number"), 1 if job.get("payment_executed") else 0,
             job.get("device_id"),
             json.dumps(job["snapshot"], ensure_ascii=False)
             if job.get("snapshot") else None))


def load_unfinished(statuses: list[str]) -> list[dict[str, Any]]:
    """Jobs the database says were still in motion, as job-shaped dicts.

    The startup sweep's eyes. Reads only what upsert_order wrote, so a job
    restored from here has no cart and no approval summary -- enough to be
    findable, announced and settled, not enough to be resumed. Resuming is
    deliberately impossible: the browser state the job depended on died
    with the process, and money must not move on a reconstruction.
    """
    marks = ",".join("?" for _ in statuses)
    with _lock, closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM orders WHERE status IN ({marks})", statuses).fetchall()
    jobs = []
    for row in rows:
        job = dict(row)
        job["items"] = json.loads(job.pop("items_json") or "[]")
        raw_snapshot = job.pop("snapshot_json", None)
        if raw_snapshot:
            job["snapshot"] = json.loads(raw_snapshot)
        job["payment_executed"] = bool(job["payment_executed"])
        jobs.append(job)
    return jobs
