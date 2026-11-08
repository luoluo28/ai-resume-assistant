"""关键词覆盖率 + 五维加权评分（确定性计算）。

- keyword_coverage_report：JD 关键词在简历中的覆盖率。
  仅借鉴 Jobscan（闭源）公开的「关键词按频次加权、计算覆盖比」通用思路，
  不逆向、不爬取、不复制其文案。
- weighted_score / verdict_band：把 LLM 打出的四维分（technical/experience/behavioral/career）
  按 30/25/15/30 加权成总分，并映射结论带；结构参考 career-ops
  （MIT License，© Santiago Fernández de Valderrama Aparicio）。
"""
from __future__ import annotations

import re
import sys


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿+#.]+", "", str(s).lower())


def _hit(kw: str, resume_raw: str) -> bool:
    """关键词命中：英文/术语按 token 精确匹配；中文词子串匹配（极短单字跳过）。"""
    kw_norm = _norm(kw)
    if not kw_norm:
        return False
    if re.fullmatch(r"[0-9a-z+#.]+", kw_norm):
        tokens = set(re.findall(r"[0-9a-z+#.]+", resume_raw.lower()))
        return kw_norm in tokens
    if len(kw_norm) < 2:
        return False
    return kw_norm in _norm(resume_raw)


def keyword_coverage_report(profile: dict, jd: dict) -> dict:
    """JD 关键词在简历（技能 + 项目 + 经验）中的覆盖率。"""
    keywords = jd.get("keywords_weighted", [])
    resume_raw = " ".join(
        [
            str(profile.get("skills", "")),
            str(profile.get("projects", "")),
            str(profile.get("experience", "")),
        ]
    )
    matched, missing = [], []
    for item in keywords:
        kw = item.get("keyword", "") if isinstance(item, dict) else str(item)
        if not kw:
            continue
        (matched if _hit(kw, resume_raw) else missing).append(kw)
    total = len(matched) + len(missing)
    coverage = round(len(matched) / total, 3) if total else 0.0
    return {
        "total_keywords": total,
        "matched": matched,
        "missing": missing,
        "coverage": coverage,
    }


WEIGHTS = {"technical": 0.30, "experience": 0.25, "behavioral": 0.15, "career": 0.30}


def weighted_score(scores: dict) -> int:
    """五维加权总分（technical 30 / experience 25 / behavioral 15 / career 30）。"""
    unknown = [k for k in scores if k not in WEIGHTS]
    if unknown:
        print(f"[warn] 未参与加权的维度被忽略: {', '.join(unknown)}", file=sys.stderr)
    missing = [k for k in WEIGHTS if k not in scores or scores[k] in (None, "")]
    if missing:
        raise ValueError(f"评分缺少维度: {missing}（不得默认 0 分，会导致总分系统性偏低）")
    total = sum(float(scores.get(k, 0) or 0) * w for k, w in WEIGHTS.items())
    return int(round(total))


def verdict_band(score: int) -> str:
    if score >= 75:
        return "Strong Fit"
    if score >= 60:
        return "Good Fit"
    if score >= 45:
        return "Moderate Fit"
    if score >= 30:
        return "Weak Fit"
    return "Poor Fit"
