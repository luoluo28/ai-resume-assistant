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


def _hit(kw: str, resume_raw: str, *, loose: bool = False) -> bool:
    """关键词命中：英文/术语按 token 精确匹配；中文词子串匹配（极短单字跳过）。

    loose=True 时，复合短语（如「Docker 容器化部署」「GitHub 代码托管」）拆开逐词判断，
    任一子词命中即算命中。理由：JD 的关键词通常是短语，简历里只写单个术语，
    严格按整串比会把明明具备的技能判成缺失（实测同一份输入覆盖率被压到 11%，
    而模型给技术维打了 85 分，两个数字自相矛盾）。
    """
    kw_norm = _norm(kw)
    if not kw_norm:
        return False
    if re.fullmatch(r"[0-9a-z+#.]+", kw_norm):
        tokens = set(re.findall(r"[0-9a-z+#.]+", resume_raw.lower()))
        return kw_norm in tokens
    if len(kw_norm) < 2:
        return False
    if kw_norm in _norm(resume_raw):
        return True
    if not loose:
        return False
    for part in re.split(r"[\s、/／（）()\[\],，·]+", str(kw)):
        part = part.strip()
        if not part or part == kw:
            continue
        if _hit(part, resume_raw):
            return True
    return False


def keyword_coverage_report(profile: dict, jd: dict, *, strict: bool = False) -> dict:
    """JD 关键词在简历（技能 + 项目 + 经验）中的覆盖率。

    strict=False（默认，用于展示给人看）：复合短语按子词判断，避免「明明会 Docker 却显示缺失」。
    strict=True（用于省钱闸门）：按整串严格判断，宁可保守也不放宽，免得批量任务多花钱。
    """
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
        (matched if _hit(kw, resume_raw, loose=not strict) else missing).append(kw)
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
