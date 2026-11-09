"""简历顾问（第 6 步）：对标 JD 优化简历 + 项目叙事 + 面试引导。

一次 LLM 调用产出 9 个块（见 prompts/06_resume_advisor.md），再经 advisor_guard 机械校验：
- 输入复用前四步的产物（profile / jd / advice），不重复调用；
- 结果按输入 sha256 缓存，同输入第二次不花钱；
- 校验丢弃的条目记在 _guard，不静默。
"""
from __future__ import annotations

import hashlib
import json
import sys

from . import advisor_guard, llm, steps

PROMPT_NAME = "06_resume_advisor.md"


def _key(resume_text: str, profile: dict, jd: dict, advice: dict, position: str) -> str:
    payload = json.dumps(
        {
            "r": resume_text,
            "p": profile,
            "j": jd,
            "a": advice,
            "pos": position,
            "v": "advisor-v2",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def resume_advisor(
    resume_text: str,
    profile: dict,
    jd: dict,
    advice: dict,
    *,
    position: str = "",
) -> dict:
    """返回顾问结果 dict；失败时返回含 error 的 dict（不抛异常）。"""
    key = _key(resume_text, profile, jd, advice, position)
    cached = steps._cache_get(key, "advisor")
    if cached is not None:
        return cached

    system = steps._fill(
        steps._load_prompt(PROMPT_NAME),
        RESUME_TEXT=resume_text,
        POSITION=position or "未提供岗位名",
        PROFILE_JSON=json.dumps(profile, ensure_ascii=False),
        JD_ANALYSIS_JSON=json.dumps(jd, ensure_ascii=False),
        APPLY_ADVICE_JSON=json.dumps(advice, ensure_ascii=False),
    )
    data = llm.chat_json(system=system, user="", temperature=0.2)
    if isinstance(data, dict) and "error" in data:
        return data

    result = advisor_guard.sanitize(
        data, resume_text=resume_text, profile=profile, position=position
    )
    steps._cache_put(key, "advisor", result)
    g = result.get("_guard", {})
    if g.get("dropped_count"):
        print(
            f"[advisor] 合规闸门丢弃 {g['dropped_count']} 条："
            + "; ".join(x.get("reason", "") for x in g.get("dropped", [])[:3]),
            file=sys.stderr,
        )
    return result
