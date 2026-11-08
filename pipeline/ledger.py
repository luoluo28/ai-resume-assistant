"""投递台账：data/ledger.jsonl，按 jd_hash 去重更新，追踪投递状态。

状态只允许：待投 / 已投 / 已回复 / 已放弃，全部由代码维护，LLM 不产出。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEDGER_FILE = DATA_DIR / "ledger.jsonl"

STATUSES = ("待投", "已投", "已回复", "已放弃")


def jd_hash(jd_text: str) -> str:
    return hashlib.sha256(jd_text.encode("utf-8")).hexdigest()[:12]


def upsert(jd_text: str, advice: dict, run_dir: str) -> None:
    """写入/更新一条台账。同一 jd_hash 只保留一条（更新其字段，保留 first_seen/status）。"""
    h = jd_hash(jd_text)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    entries = _read()
    existing = next((e for e in entries if e.get("jd_hash") == h), None)
    entry = {
        "position": advice.get("position", ""),
        "score": advice.get("score", ""),
        "verdict": advice.get("verdict", ""),
        "location_verdict": advice.get("location_verdict", ""),
        "conclusion": advice.get("conclusion", ""),
        "summary_path": str(Path(run_dir) / "04_apply_advice.json"),
        "status": existing["status"] if existing else "待投",
        "first_seen": existing["first_seen"] if existing else now,
        "last_seen": now,
        "jd_hash": h,
    }
    if existing:
        entries = [entry if e.get("jd_hash") == h else e for e in entries]
    else:
        entries.append(entry)
    _write(entries)


def list_entries(status: str | None = None) -> list[dict]:
    entries = _read()
    if status:
        entries = [e for e in entries if e.get("status") == status]
    entries.sort(key=lambda e: float(e.get("score") or 0), reverse=True)
    return entries


def set_status(position: str, status: str, jd_hash: str | None = None) -> int:
    """改状态。jd_hash 指定唯一条目；否则按 position 匹配，多条时报错要求指定。"""
    if status not in STATUSES:
        raise ValueError(f"status 只允许 {STATUSES}")
    entries = _read()
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
    matched[0]["status"] = status
    matched[0]["status_changed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    _write(entries)
    return 1


def stats() -> dict:
    out = {s: 0 for s in STATUSES}
    for e in _read():
        out[e.get("status", "待投")] = out.get(e.get("status", "待投"), 0) + 1
    return out


def _read() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    out = []
    for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write(entries: list[dict]) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )
