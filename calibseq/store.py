"""SQLite 持久化：模式、连接与事务助手。

并发模型：所有写路径都在 BEGIN IMMEDIATE 事务内串行执行，
判定先封存快照再计算，补报与判定的并发由同一份封存读数裁决。
"""
from __future__ import annotations

import sqlite3
import threading
import weakref
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  name TEXT NOT NULL,
  overall_risk REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN'
    CHECK (status IN ('OPEN', 'RELEASED', 'MANUAL_REVIEW')),
  current_round INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS zones (
  case_id INTEGER NOT NULL REFERENCES cases(id),
  zone_id TEXT NOT NULL,
  name TEXT NOT NULL,
  lower REAL NOT NULL,
  upper REAL NOT NULL,
  PRIMARY KEY (case_id, zone_id)
);
CREATE TABLE IF NOT EXISTS rounds (
  case_id INTEGER NOT NULL REFERENCES cases(id),
  round_no INTEGER NOT NULL,
  cumulative_n INTEGER NOT NULL,
  PRIMARY KEY (case_id, round_no)
);
CREATE TABLE IF NOT EXISTS measurements (
  case_id INTEGER NOT NULL REFERENCES cases(id),
  record_id TEXT NOT NULL,
  zone_id TEXT NOT NULL,
  value REAL NOT NULL,
  ts TEXT NOT NULL,
  PRIMARY KEY (case_id, record_id)
);
CREATE TABLE IF NOT EXISTS judgment_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id),
  round_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('SEALED', 'COMPLETED')),
  quota INTEGER NOT NULL,
  alpha_round REAL NOT NULL,
  snapshot TEXT NOT NULL,
  result TEXT,
  decision TEXT CHECK (decision IN ('PASS', 'CONTINUE', 'MANUAL_REVIEW')),
  sealed_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_measurements_case_zone ON measurements(case_id, zone_id);
CREATE INDEX IF NOT EXISTS idx_runs_case_status ON judgment_runs(case_id, status);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """打开（必要时创建）数据库并确保模式存在。"""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # 内存库不支持 WAL，忽略
    conn.executescript(SCHEMA)
    return conn


_CONN_LOCKS: dict = {}
_CONN_LOCKS_GUARD = threading.Lock()


def conn_lock(conn: sqlite3.Connection) -> threading.RLock:
    """每连接一把可重入锁：多线程共享同一连接时串行化其事务。"""
    key = id(conn)
    with _CONN_LOCKS_GUARD:
        entry = _CONN_LOCKS.get(key)
        if entry is not None and entry[0]() is conn:
            return entry[1]
        try:
            ref = weakref.ref(conn)
        except TypeError:  # 极少数构建不支持弱引用
            ref = lambda: conn  # noqa: E731
        lock = threading.RLock()
        _CONN_LOCKS[key] = (ref, lock)
        return lock


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 事务：写操作串行化，保证封存/判定/补报的并发语义。"""
    lock = conn_lock(conn)
    with lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
