"""SQLite: thiết bị, phòng, phiên thu, nhãn, sự kiện, phiên nền, tác vụ huấn luyện, mô hình, phát hành.

Chuỗi mẫu không nằm trong SQLite mà trong file CSV của từng phiên (data/sessions/<id>/samples.csv).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY, name TEXT NOT NULL, notes TEXT DEFAULT '', created_at INTEGER
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY, name TEXT DEFAULT '', room_id TEXT, created_at INTEGER, last_seen_ms INTEGER,
    online INTEGER DEFAULT 0, status_json TEXT, fw_version TEXT, active_model_id TEXT, active_model_version INTEGER,
    config_json TEXT, config_rev INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS device_clock (
    device_id TEXT PRIMARY KEY, boot_id INTEGER NOT NULL, offset_ms REAL NOT NULL, dev_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, room_id TEXT, kind TEXT NOT NULL, source TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL, ended_at_ms INTEGER, status TEXT NOT NULL, notes TEXT DEFAULT '',
    radar_json TEXT, split TEXT, samples INTEGER DEFAULT 0, planned_end_ms INTEGER
);
CREATE INDEX IF NOT EXISTS sessions_device ON sessions(device_id, status);
CREATE TABLE IF NOT EXISTS labels (
    label_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, start_ms INTEGER NOT NULL,
    end_ms INTEGER, label TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'human', note TEXT DEFAULT '',
    created_at INTEGER, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS labels_session ON labels(session_id, start_ms);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, session_id TEXT, ts_ms INTEGER NOT NULL,
    kind TEXT NOT NULL, note TEXT DEFAULT '', data_json TEXT, created_at INTEGER
);
CREATE INDEX IF NOT EXISTS events_device ON events(device_id, ts_ms);
CREATE TABLE IF NOT EXISTS baselines (
    baseline_id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, room_id TEXT, session_id TEXT,
    name TEXT NOT NULL, condition TEXT DEFAULT '', disturbance INTEGER DEFAULT 0, created_at INTEGER,
    status TEXT NOT NULL, stats_json TEXT, thresholds_json TEXT, warnings_json TEXT, active INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, started_at INTEGER, finished_at INTEGER,
    status TEXT NOT NULL, request_json TEXT, progress_json TEXT, error TEXT, log_path TEXT, model_id TEXT,
    dataset_path TEXT
);
CREATE TABLE IF NOT EXISTS models (
    model_id TEXT PRIMARY KEY, model_version INTEGER UNIQUE NOT NULL, created_at INTEGER, job_id INTEGER,
    status TEXT NOT NULL, dir TEXT NOT NULL, package_sha256 TEXT, package_size INTEGER, signed INTEGER,
    simulated INTEGER, conclusive INTEGER, summary_json TEXT, approved_at INTEGER, note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS releases (
    release_id INTEGER PRIMARY KEY AUTOINCREMENT, model_id TEXT NOT NULL, device_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'release', created_at INTEGER, updated_at INTEGER, status TEXT NOT NULL,
    error TEXT, detail_json TEXT
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Database:
    """Kết nối SQLite dùng chung giữa các luồng (khóa toàn cục: tải nhẹ, một người dùng)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def execute(self, sql: str, args: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    def one(self, sql: str, args: tuple | list = ()) -> dict | None:
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def all(self, sql: str, args: tuple | list = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def transaction(self):
        return _Tx(self)


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()
        return False


def jloads(s: str | None, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def jdumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
