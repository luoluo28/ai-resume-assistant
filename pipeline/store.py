"""轻量持久化：SQLite（Python 自带），只存"任务索引"，不存大文本。

表 tasks:
  task_id     TEXT PRIMARY KEY
  created_at  TEXT   # YYYY-MM-DD HH:MM:SS
  status      TEXT   # running / done / error
  step        INTEGER
  elapsed     REAL
  error       TEXT
  run_dir     TEXT   # data/runs/<ts>，结果 JSON 都在这
  resume_name TEXT
  position    TEXT

设计取舍：五步 JSON、简历/JD 原文仍留在 run 目录（文件系统），
数据库只解决"重启后不知道跑过什么"的问题，避免把大文本塞进库。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "app.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    step        INTEGER NOT NULL DEFAULT 0,
    elapsed     REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    run_dir     TEXT NOT NULL DEFAULT '',
    resume_name TEXT NOT NULL DEFAULT '',
    position    TEXT NOT NULL DEFAULT ''
);
"""


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def init() -> None:
    with _conn():
        pass


def save_task(task_id: str, **fields) -> None:
    cols = [
        "status",
        "step",
        "elapsed",
        "error",
        "run_dir",
        "resume_name",
        "position",
    ]
    values = {k: fields.get(k) for k in cols if k in fields}
    with _conn() as conn:
        row = conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tasks (task_id, created_at) VALUES (?, ?)",
                (task_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        if values:
            assignments = ", ".join(f"{k} = ?" for k in values)
            conn.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id = ?",
                (*values.values(), task_id),
            )


def get_task(task_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def list_tasks(limit: int = 10) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
