"""投递台账：SQLite ledger 表（从 data/ledger.jsonl 一次性迁移来），按 jd_hash 去重更新，追踪投递状态。

状态只允许：待投 / 已投 / 已回复 / 已放弃，全部由代码维护，LLM 不产出。
四个公开函数 upsert / list_entries / set_status / stats 签名不变，只换底层存储。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from . import store

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEDGER_FILE = DATA_DIR / "ledger.jsonl"

STATUSES = ("待投", "已投", "已回复", "已放弃")

_migrated = False


def jd_hash(jd_text: str) -> str:
    return hashlib.sha256(jd_text.encode("utf-8")).hexdigest()[:12]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _migrate() -> None:
    """一次性迁移：jsonl 存在且 ledger 表为空 → 逐行导入 device_id='local' 并改名留证据；
    jsonl 与数据库都有数据 → 以数据库为准，打印提示，不静默覆盖。"""
    global _migrated
    if _migrated:
        return
    _migrated = True
    if not LEDGER_FILE.exists():
        return
    if store.ledger_count() > 0:
        print("[ledger] jsonl 与数据库都有数据，以数据库为准（jsonl 不再写入）", file=sys.stderr)
        return
    imported = 0
    for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        store.ledger_upsert(
            jd_hash=e.get("jd_hash", ""),
            device_id="local",
            position=e.get("position", ""),
            score=_num(e.get("score", "")),
            verdict=e.get("verdict", ""),
            location_verdict=e.get("location_verdict", ""),
            conclusion=e.get("conclusion", ""),
            summary_path=e.get("summary_path", ""),
            status=e.get("status", "待投"),
            first_seen=e.get("first_seen", ""),
            last_seen=e.get("last_seen", ""),
            status_changed_at=e.get("status_changed_at", ""),
        )
        imported += 1
    LEDGER_FILE.rename(LEDGER_FILE.with_suffix(".jsonl.migrated"))
    print(f"[ledger] 已把 {imported} 条 jsonl 迁移到数据库，原文件改名 ledger.jsonl.migrated", file=sys.stderr)


def upsert(jd_text: str, advice: dict, run_dir: str) -> None:
    """写入/更新一条台账。同一 jd_hash 只保留一条（更新字段，保留 first_seen/status）。"""
    _migrate()
    h = jd_hash(jd_text)
    store.ledger_upsert(
        jd_hash=h,
        device_id="local",
        position=advice.get("position", ""),
        score=_num(advice.get("score", "")),
        verdict=advice.get("verdict", ""),
        location_verdict=advice.get("location_verdict", ""),
        conclusion=advice.get("conclusion", ""),
        summary_path=str(Path(run_dir) / "04_apply_advice.json"),
    )


def list_entries(status: str | None = None) -> list[dict]:
    _migrate()
    entries = store.ledger_list(status)
    entries.sort(key=lambda e: float(e.get("score") or 0), reverse=True)
    return entries


def set_status(position: str, status: str, jd_hash: str | None = None) -> int:
    """改状态。jd_hash 指定唯一条目；否则按 position 匹配，多条时报错要求指定。"""
    _migrate()
    if status not in STATUSES:
        raise ValueError(f"status 只允许 {STATUSES}")
    entries = store.ledger_list()
    if jd_hash:
        matched = [e for e in entries if e.get("jd_hash") == jd_hash]
        label = f"jd_hash={jd_hash}"
    else:
        matched = [e for e in entries if e.get("position") == position]
        label = f"position={position}"
    if not matched:
        raise ValueError(f"未找到 {label} 的台账条目")
    if len(matched) > 1:
        hashes = "、".join(e.get("jd_hash", "") for e in matched)
        raise ValueError(f"匹配到 {len(matched)} 条同名岗位，请用 --jd-hash 指定其一：{hashes}")
    store.ledger_set_status(matched[0]["jd_hash"], status)
    return 1


def stats() -> dict:
    _migrate()
    out = {s: 0 for s in STATUSES}
    for s, n in store.ledger_stats().items():
        out[s] = out.get(s, 0) + n
    return out
