"""评分与覆盖率（确定性计算，非 LLM）。

- keyword_coverage：JD 关键词在简历中的覆盖率。
  仅借鉴 Jobscan（闭源）公开的「关键词按频次加权、计算覆盖比」通用思路，
  不逆向、不爬取、不复制其文案。
- score_dimensions：五维评分（技能 / 经验 / 学历 / 软实力 / 文化契合）。
  评分结构参考 career-ops（MIT License，© Santiago Fernández de Valderrama Aparicio），
  实现为原创启发式，未复制其代码。
"""
from __future__ import annotations

import re

DIMENSION_NAMES = ["技能匹配", "经验匹配", "学历匹配", "软实力", "文化契合"]


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿+#.]+", "", str(s).lower())


def keyword_coverage(profile: dict, jd: dict) -> dict:
    """JD 关键词在简历（技能 + 项目 + 经验）中的覆盖率。"""
    keywords = jd.get("keywords_weighted", [])
    resume = _norm(
        " ".join(
            [
                str(profile.get("skills", "")),
                str(profile.get("projects", "")),
                str(profile.get("experience", "")),
            ]
        )
    )
    matched, missing = [], []
    for item in keywords:
        kw = item.get("keyword", "") if isinstance(item, dict) else str(item)
        if not kw:
            continue
        (matched if (_norm(kw) and _norm(kw) in resume) else missing).append(kw)
    total = len(matched) + len(missing)
    coverage = round(len(matched) / total, 3) if total else 0.0
    return {
        "total_keywords": total,
        "matched": matched,
        "missing": missing,
        "coverage": coverage,
    }


def score_dimensions(profile: dict, jd: dict, values: dict | None = None) -> dict:
    cov = keyword_coverage(profile, jd)
    skills = int(cov["coverage"] * 100)

    exp = str(profile.get("experience", ""))
    exp_score = 60 if any(ch.isdigit() for ch in exp) else 30

    edu_score = 70 if str(profile.get("education", "")) else 20

    soft = jd.get("soft_skills", [])
    proj = str(profile.get("projects", ""))
    soft_score = 60 if (soft and proj) else 30

    culture_score = 60 if (values and values.get("labels")) else 40

    dims = {
        "技能匹配": skills,
        "经验匹配": exp_score,
        "学历匹配": edu_score,
        "软实力": soft_score,
        "文化契合": culture_score,
    }
    overall = round(sum(dims.values()) / len(dims), 1)
    return {"dimensions": dims, "overall": overall}


def compute(profile: dict, jd: dict, values: dict | None = None) -> dict:
    return {
        "keyword_coverage": keyword_coverage(profile, jd),
        "score": score_dimensions(profile, jd, values),
    }
