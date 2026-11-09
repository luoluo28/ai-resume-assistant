"""DeepSeek 官方 API 调用封装（OpenAI 兼容 /chat/completions）。

- 密钥从环境变量 DEEPSEEK_API_KEY 读取，绝不硬编码、绝不给默认值。
- 仓库根目录若有 .env 会自动读取（不覆盖已存在的环境变量）。
- 调用时用 response_format=json_object；解析失败时返回带 error 的兜底 dict，不抛异常。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# ---- 调用计量：批量跑完能看见"到底花了几次、多少字" ----
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CALL_LOG = DATA_DIR / "api_calls.jsonl"
_STATS = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "prompt_chars": 0, "cache_hits": 0}
_LIMIT = 0


class BudgetExceeded(Exception):
    """真实调用次数已达上限（--budget-calls）。"""


def set_call_limit(n: int) -> None:
    global _LIMIT
    _LIMIT = max(0, int(n or 0))


def budget_left() -> int | None:
    if not _LIMIT:
        return None
    return max(0, _LIMIT - _STATS["calls"])


def note_cache_hit() -> None:
    _STATS["cache_hits"] += 1


def stats() -> dict:
    return dict(_STATS)


def reset_stats() -> None:
    for k in _STATS:
        _STATS[k] = 0


def _log_call(step: str, prompt_chars: int, usage: dict) -> None:
    _STATS["calls"] += 1
    _STATS["prompt_chars"] += prompt_chars
    _STATS["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
    _STATS["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with CALL_LOG.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "step": step,
                        "prompt_chars": prompt_chars,
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


def _api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError(
            "缺少环境变量 DEEPSEEK_API_KEY。请在本地 .env 填真实密钥后重新运行；"
            "本仓库为 public，密钥绝不写入代码或提交。"
        )
    return key


def chat_json(system: str, user: str = "", *, temperature: float = 0.1, max_tokens: int = 0, step: str = "") -> dict:
    """调用 DeepSeek 并要求 JSON 输出，返回解析后的 dict（失败时含 error 字段）。

    max_tokens=0 时不限制；step 仅用于计量日志（如 "04_apply_advice"）。
    """
    if _LIMIT and _STATS["calls"] >= _LIMIT:
        raise BudgetExceeded(f"已达本次调用上限 {_LIMIT} 次，停止调用")
    url = f"{BASE_URL}/chat/completions"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {_api_key()}"},
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    _log_call(step or "chat_json", len(system) + len(user), body.get("usage") or {})
    return parse_json(content)


def parse_json(text: str) -> dict:
    """尽力把模型输出解析成 JSON dict；失败时返回 {'error': ..., 'raw': ...}。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                data = None
        else:
            data = None

    if isinstance(data, dict):
        return data
    return {"error": "JSON 解析失败", "raw": text[:500]}
