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

-- V3（FR9）：复习卡两张新表。迁移策略 additive-only —— 全部
-- CREATE TABLE IF NOT EXISTS，老库下次启动自动补建，无 ALTER、无停机。
-- cards.fsrs 是唯一事实源（官方 Card.to_dict() 的 JSON），due 只是为队列
-- 查询冗余的排序列，两列都只经 app/review.py 的写入口赋值。
CREATE TABLE IF NOT EXISTS cards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction_id INTEGER NOT NULL REFERENCES directions(id) ON DELETE CASCADE,
    front        TEXT NOT NULL,
    back         TEXT NOT NULL DEFAULT '',
    fsrs         TEXT NOT NULL,
    due          TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 该表唯一时间列是 reviewed_at（UTC ISO 8601）；刻意不设 created_at，
-- 避免同一张表里混着两种时区口径的列（docs/06 §3 决议）。
CREATE TABLE IF NOT EXISTS review_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    rating      INTEGER NOT NULL CHECK (rating IN (1, 2, 3, 4)),
    reviewed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_dir_date ON logs(direction_id, date);
CREATE INDEX IF NOT EXISTS idx_tasks_phase   ON tasks(phase_id);
CREATE INDEX IF NOT EXISTS idx_phases_dir    ON phases(direction_id);
CREATE INDEX IF NOT EXISTS idx_cards_dir_due      ON cards(direction_id, due);
CREATE INDEX IF NOT EXISTS idx_review_logs_card   ON review_logs(card_id, reviewed_at);
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
