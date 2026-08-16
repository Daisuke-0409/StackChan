"""Order history and job audit trail (FR-6 / NFR-1).

Every job transition is recorded, whether or not money moved: the audit
table is the answer to "did it click pay or not" when anything is ever in
doubt, so writes here happen BEFORE the action they describe as well as
after its outcome is known.
"""
from __future__ import annotations

import json
import sqlite3
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
    payment_executed INTEGER NOT NULL DEFAULT 0
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
    return conn


def record(job_id: str, event: str, detail: Optional[dict[str, Any]] = None) -> None:
    with _lock, _connect() as conn:
        conn.execute("INSERT INTO audit (job_id, at, event, detail_json) VALUES (?, ?, ?, ?)",
                     (job_id, time.time(), event, json.dumps(detail or {}, ensure_ascii=False)))


def upsert_order(job: dict[str, Any]) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO orders (job_id, created_at, chain, store_key, store_name,
                                   items_json, pickup, total_yen, status, order_number, payment_executed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                   store_key=excluded.store_key, store_name=excluded.store_name,
                   items_json=excluded.items_json, pickup=excluded.pickup,
                   total_yen=excluded.total_yen, status=excluded.status,
                   order_number=excluded.order_number,
                   payment_executed=excluded.payment_executed""",
            (job["job_id"], job["created_at"], job["chain"], job.get("store_key"),
             job.get("store_name"), json.dumps(job.get("items", []), ensure_ascii=False),
             job.get("pickup"), job.get("total_yen"), job["status"],
             job.get("order_number"), 1 if job.get("payment_executed") else 0))
