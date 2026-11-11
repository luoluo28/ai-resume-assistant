"""轻量持久化：SQLite（Python 自带），只存索引与状态，不存大文本。

表：
  tasks    任务索引（谁、跑到哪、结果 run_dir）
  users    设备（cookie 里的 device_id，不是 IP、不是 key）
  sessions 一次分析（device + resume_sha + jd_sha + position 唯一，用于省钱复用）
  messages 追问/多轮对话（本批只存不读）
  ledger   投递台账（从 data/ledger.jsonl 迁移来）

设计取舍：五步 JSON、简历/JD 原文仍留在 run 目录（文件系统），
数据库只解决"重启后不知道跑过什么、谁跑的"。
"""
from __future__ import annotations

import sqlite3
import uuid
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
CREATE TABLE IF NOT EXISTS users (
    device_id   TEXT PRIMARY KEY,          -- cookie 里的 uuid4 hex，不是 IP、不是 key
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL DEFAULT '',
    plan        TEXT NOT NULL DEFAULT 'local',   -- local / guest
    daily_date  TEXT NOT NULL DEFAULT '',         -- YYYY-MM-DD，用于每日计数
    daily_count INTEGER NOT NULL DEFAULT 0        -- T18 的上限直接用这两列
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,        -- uuid4 hex
    device_id    TEXT NOT NULL,
    task_id      TEXT NOT NULL DEFAULT '',
    resume_sha   TEXT NOT NULL,           -- sha256(简历原文)[:16]
    jd_sha       TEXT NOT NULL DEFAULT '',-- sha256(JD原文)[:16]
    position     TEXT NOT NULL DEFAULT '',
    score        REAL,
    verdict      TEXT NOT NULL DEFAULT '',
    conclusion   TEXT NOT NULL DEFAULT '',
    venue        TEXT NOT NULL DEFAULT '',-- location_verdict，便于历史页筛"该投/别投"
    run_dir      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    UNIQUE(device_id, resume_sha, jd_sha, position)
);
CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id, created_at DESC);
CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    session_id  TEXT NOT NULL,          -- 关联 sessions.session_id
    role        TEXT NOT NULL,          -- user / assistant
    content     TEXT NOT NULL,
    tokens      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(device_id, session_id, created_at);
CREATE TABLE IF NOT EXISTS ledger (
    jd_hash          TEXT PRIMARY KEY,
    device_id        TEXT NOT NULL DEFAULT 'local',
    position         TEXT NOT NULL DEFAULT '',
    score            REAL,
    verdict          TEXT NOT NULL DEFAULT '',
    location_verdict TEXT NOT NULL DEFAULT '',
    conclusion       TEXT NOT NULL DEFAULT '',
    summary_path     TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT '待投',
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    status_changed_at TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧 tasks 表补 device_id 列（存量数据一律填 'local'）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "device_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN device_id TEXT NOT NULL DEFAULT 'local'")


def init() -> None:
    with _conn():
        pass


# ---------------- users ----------------

def get_or_create_user(device_id: str) -> None:
    with _conn() as conn:
        row = conn.execute("SELECT device_id FROM users WHERE device_id = ?", (device_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (device_id, created_at, last_seen) VALUES (?, ?, ?)",
                (device_id, _now(), _now()),
            )
        else:
            conn.execute("UPDATE users SET last_seen = ? WHERE device_id = ?", (_now(), device_id))


def incr_daily_count(device_id: str, limit: int) -> bool:
    """今日真实调用 +1；跨天自动重置；已达上限返回 False 不 +1。"""
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as conn:
        row = conn.execute("SELECT daily_date, daily_count FROM users WHERE device_id = ?", (device_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (device_id, created_at, last_seen, daily_date, daily_count) VALUES (?,?,?,?,1)",
                (device_id, _now(), _now(), today),
            )
            return True
        if row["daily_date"] != today:
            conn.execute("UPDATE users SET daily_date=?, daily_count=1 WHERE device_id=?", (today, device_id))
            return True
        if row["daily_count"] >= limit:
            return False
        conn.execute("UPDATE users SET daily_count = daily_count + 1 WHERE device_id=?", (device_id,))
        return True


def over_daily_limit(device_id: str, limit: int) -> bool:
    """只看不增：是否已超今日上限。"""
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as conn:
        row = conn.execute("SELECT daily_date, daily_count FROM users WHERE device_id = ?", (device_id,)).fetchone()
    if row is None or row["daily_date"] != today:
        return False
    return row["daily_count"] >= limit


# ---------------- tasks ----------------

def save_task(task_id: str, **fields) -> None:
    cols = ["status", "step", "elapsed", "error", "run_dir", "resume_name", "position", "device_id"]
    values = {k: fields.get(k) for k in cols if k in fields}
    with _conn() as conn:
        row = conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tasks (task_id, created_at, device_id) VALUES (?, ?, ?)",
                (task_id, _now(), values.get("device_id", "local")),
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
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_tasks_for_device(device_id: str, limit: int = 10) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE device_id = ? ORDER BY created_at DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------- sessions ----------------

def save_session(device_id: str, resume_sha: str, jd_sha: str, position: str,
                 advice: dict, run_dir: str, task_id: str = "") -> str:
    """写入/更新一条 session；UNIQUE(device, resume_sha, jd_sha, position) 保证同输入只有一条。"""
    now = _now()
    score = advice.get("score")
    verdict = advice.get("verdict", "")
    conclusion = advice.get("conclusion", "")
    venue = advice.get("location_verdict", "")
    with _conn() as conn:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE device_id=? AND resume_sha=? AND jd_sha=? AND position=?",
            (device_id, resume_sha, jd_sha, position),
        ).fetchone()
        if row is None:
            session_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO sessions (session_id, device_id, task_id, resume_sha, jd_sha, position, "
                "score, verdict, conclusion, venue, run_dir, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, device_id, task_id, resume_sha, jd_sha, position,
                 score, verdict, conclusion, venue, run_dir, now),
            )
            return session_id
        session_id = row["session_id"]
        conn.execute(
            "UPDATE sessions SET task_id=?, score=?, verdict=?, conclusion=?, venue=?, run_dir=?, created_at=? "
            "WHERE session_id=?",
            (task_id, score, verdict, conclusion, venue, run_dir, now, session_id),
        )
        return session_id


def get_session_by_keys(device_id: str, resume_sha: str, jd_sha: str, position: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE device_id=? AND resume_sha=? AND jd_sha=? AND position=?",
            (device_id, resume_sha, jd_sha, position),
        ).fetchone()
    return dict(row) if row else None


def get_session(session_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(device_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE device_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (device_id, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def list_sessions_multi(device_ids: list, limit: int = 20, offset: int = 0) -> list[dict]:
    """按多个 device_id 查历史（当前设备 + 本机时的 'local'）。"""
    if not device_ids:
        return []
    placeholders = ",".join("?" for _ in device_ids)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM sessions WHERE device_id IN ({placeholders}) ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*device_ids, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def device_has_resume(device_id: str, resume_sha: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE device_id=? AND resume_sha=? LIMIT 1",
            (device_id, resume_sha),
        ).fetchone()
    return row is not None


# ---------------- messages ----------------

def add_message(device_id: str, session_id: str, role: str, content: str, tokens: int = 0) -> str:
    mid = uuid.uuid4().hex
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (message_id, device_id, session_id, role, content, tokens, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (mid, device_id, session_id, role, content, tokens, _now()),
        )
    return mid


# ---------------- ledger（供 ledger.py 用，迁移后不再读写 jsonl） ----------------

def ledger_upsert(jd_hash: str, device_id: str, position: str, score, verdict: str,
                  location_verdict: str, conclusion: str, summary_path: str,
                  status: str = "待投", first_seen: str = "", last_seen: str = "",
                  status_changed_at: str = "") -> None:
    """插入/更新一条台账。插入时可用 status/first_seen 回填迁移数据；
    更新时保留 status 与 first_seen（同原 jsonl 行为），只刷新 last_seen。"""
    now = _now()
    first_seen = first_seen or now
    last_seen = last_seen or now
    with _conn() as conn:
        row = conn.execute("SELECT * FROM ledger WHERE jd_hash = ?", (jd_hash,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO ledger (jd_hash, device_id, position, score, verdict, location_verdict, "
                "conclusion, summary_path, status, first_seen, last_seen, status_changed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (jd_hash, device_id, position, score, verdict, location_verdict, conclusion,
                 summary_path, status, first_seen, last_seen, status_changed_at),
            )
        else:
            conn.execute(
                "UPDATE ledger SET device_id=?, position=?, score=?, verdict=?, location_verdict=?, "
                "conclusion=?, summary_path=?, last_seen=? WHERE jd_hash=?",
                (device_id, position, score, verdict, location_verdict, conclusion, summary_path, now, jd_hash),
            )


def ledger_list(status: str | None = None) -> list[dict]:
    with _conn() as conn:
        if status:
            rows = conn.execute("SELECT * FROM ledger WHERE status=? ORDER BY score DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ledger ORDER BY score DESC").fetchall()
    return [dict(r) for r in rows]


def ledger_set_status(jd_hash: str, status: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE ledger SET status=?, status_changed_at=? WHERE jd_hash=?",
            (status, _now(), jd_hash),
        )


def ledger_stats() -> dict:
    out = {}
    with _conn() as conn:
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM ledger GROUP BY status"):
            out[r["status"]] = r["n"]
    return out


def ledger_get(jd_hash: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM ledger WHERE jd_hash = ?", (jd_hash,)).fetchone()
    return dict(row) if row else None


def ledger_count() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
