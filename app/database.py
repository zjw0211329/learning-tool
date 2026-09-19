"""SQLite 连接管理与建库建表。

数据模型见 docs/03-概要设计.md 第 3 节。
"""
import os
import sqlite3

from flask import g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "study.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS directions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS phases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction_id INTEGER NOT NULL REFERENCES directions(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    goal         TEXT NOT NULL DEFAULT '',
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phase_id   INTEGER NOT NULL REFERENCES phases(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'todo'
               CHECK (status IN ('todo', 'doing', 'done', 'skipped')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    done_at    TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction_id INTEGER NOT NULL REFERENCES directions(id) ON DELETE CASCADE,
    date         TEXT NOT NULL,
    minutes      INTEGER NOT NULL DEFAULT 0,
    content      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_logs_dir_date ON logs(direction_id, date);
CREATE INDEX IF NOT EXISTS idx_tasks_phase   ON tasks(phase_id);
CREATE INDEX IF NOT EXISTS idx_phases_dir    ON phases(direction_id);
"""


def get_db() -> sqlite3.Connection:
    """取当前请求的数据库连接（每请求一个，自动开外键级联）。"""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """首次运行时建目录、建表。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
