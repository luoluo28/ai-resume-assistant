"""机械预筛：完全不调模型，先用零成本规则砍掉大部分岗位。

三道筛：
1. 去重：公司+岗位名+城市+JD 归一化 sha256，同岗多平台重复只算一次；
2. 硬性否决：城市不符 / 要求硕士而简历没有 / 要求 N 年经验而简历无经验；
3. 技能覆盖率：把简历 skills 拆成词条，看有多少在 JD 原文里出现，按覆盖率打分。

输出每条岗位带 score（0–100）、coverage、matched、reasons，便于用户先看表再决定要不要花钱。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_NUM_YEARS = re.compile(r"(\d+)\s*年(?:以上|及以上)?(?:工作)?经验|(\d+)\s*年以上")
_MASTER = ("硕士", "研究生", "博士")
_CITY_PAREN = re.compile(r"[（(]\s*([\u4e00-\u9fa5]{2,8})\s*[）)]")
_SPLIT = re.compile(r"[,，、/;；|\s]+")
# 括号里常见但不是地名 —— 避免把「（校招）（实习）」当成城市
_NOT_CITY = ("校招", "社招", "实习", "全职", "兼职", "应届", "急招", "急聘", "远程", "外包", "派遣", "可转正")
# 2-gram 里的通用词：命中它们不代表技能相关
_STOP_GRAM = {
    "负责", "参与", "进行", "完成", "使用", "相关", "能力", "经验", "工作", "要求",
    "职责", "能够", "熟悉", "了解", "掌握", "公司", "团队", "岗位", "优秀", "具备",
}


def norm_text(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"\s+", "", t)


def fingerprint(job: dict) -> str:
    jd = norm_text(job.get("jd", ""))
    payload = "|".join(
        [
            norm_text(job.get("company", "")),
            norm_text(job.get("title", "")),
            norm_text(job.get("city", "")),
            hashlib.sha256(jd.encode("utf-8")).hexdigest()[:16],
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def city_of(job: dict) -> str:
    """城市：优先显式 city 字段；否则从标题括号里取，且必须不是「校招/实习」这类非地名。"""
    explicit = str(job.get("city", "") or "").strip()
    if explicit:
        return explicit
    m = _CITY_PAREN.search(str(job.get("title", "") or ""))
    if not m:
        return ""
    cand = m.group(1)
    if cand in _NOT_CITY or any(k in cand for k in _NOT_CITY):
        return ""
    return cand


def _hit(term: str, jd_norm: str) -> bool:
    """宽松命中：英文整词子串；中文要求整词命中或≥2 个不同 2-gram 命中（避免「系统」这种单字噪声）。"""
    t = norm_text(term)
    if not t:
        return False
    if t in jd_norm:
        return True
    if t.isascii() or len(t) <= 3:
        return False
    grams = {t[i : i + 2] for i in range(len(t) - 1)} - _STOP_GRAM
    return sum(1 for g in grams if g in jd_norm) >= 2


def skill_terms(profile: dict) -> list[str]:
    """简历 skills 拆词条；英文按词，中文按分隔符切出的短语（≥2 字）。"""
    raw = str((profile or {}).get("skills", "") or "")
    terms = []
    for piece in _SPLIT.split(raw):
        p = piece.strip()
        if len(p) >= 2 and p.lower() not in ("and", "or"):
            terms.append(p)
    for piece in _SPLIT.split(str((profile or {}).get("projects", "") or "")):
        p = piece.strip()
        if p.isascii() and len(p) >= 2:
            terms.append(p)
    seen, out = set(), []
    for t in terms:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def skill_overlap(profile: dict, jd_text: str) -> tuple[list[str], int, float]:
    terms = skill_terms(profile)
    jd_norm = norm_text(jd_text)
    if not terms:
        return [], 0, 0.0
    matched = [t for t in terms if _hit(t, jd_norm)]
    return matched, len(terms), round(len(matched) / len(terms), 3)


def overlap_score(matched: int) -> int:
    """命中 1 项=20、2 项=40、3 项=60、5 项及以上=100（分母不该是简历全部技能数）。"""
    return min(100, 20 * matched)


def veto_reasons(job: dict, profile: dict, *, city: str = "") -> list[str]:
    reasons = []
    jd = str(job.get("jd", "") or "")
    job_city = city_of(job)
    if city and job_city and city not in job_city and job_city not in city:
        reasons.append(f"城市不符（岗位 {job_city} / 目标 {city}）")

    edu = str((profile or {}).get("education", "") or "")
    if any(m in jd for m in _MASTER) and not any(m in edu for m in _MASTER):
        reasons.append("要求硕士及以上")

    exp = str((profile or {}).get("experience", "") or "")
    for m in _NUM_YEARS.finditer(jd):
        years = int(m.group(1) or m.group(2) or 0)
        if years >= 3 and len(exp.strip()) < 20:
            reasons.append(f"要求 {years} 年经验")
            break
    return reasons


def prefilter(
    jobs: list[tuple[int, dict]],
    profile: dict,
    *,
    city: str = "",
    min_score: int = 60,
    min_coverage: float = 0.0,
) -> dict:
    """返回 {"pass": [...], "drop": [...], "dupes": [...]}，每条带打分与原因。"""
    seen: dict = {}
    passed, dropped, dupes = [], [], []
    for i, job in jobs:
        fp = fingerprint(job)
        if fp in seen:
            dupes.append(
                {"index": i, "title": job.get("title", ""), "fingerprint": fp, "same_as_index": seen[fp]}
            )
            continue
        seen[fp] = i

        reasons = veto_reasons(job, profile, city=city)
        matched, total, cov = skill_overlap(profile, job.get("jd", ""))
        score = overlap_score(len(matched))
        if reasons:
            dropped.append(
                {
                    "index": i,
                    "title": job.get("title", ""),
                    "city": city_of(job),
                    "score": 0,
                    "coverage": cov,
                    "matched": len(matched),
                    "terms": total,
                    "reasons": reasons,
                }
            )
            continue
        rec = {
            "index": i,
            "title": job.get("title", ""),
            "city": city_of(job),
            "score": score,
            "coverage": cov,
            "matched": len(matched),
            "terms": total,
            "matched_terms": matched[:12],
            "reasons": [],
        }
        if score < min_score:
            rec["reasons"] = [f"技能匹配分 {score} 低于阈值 {min_score}（命中 {len(matched)} 项）"]
            dropped.append(rec)
        else:
            passed.append(rec)
    passed.sort(key=lambda x: -x["score"])
    return {"pass": passed, "drop": dropped, "dupes": dupes}


def gate_after_jd(jd: dict, profile: dict, *, min_coverage: float = 0.0) -> tuple[bool, str]:
    """两段漏斗的第二段：02 出来后判断要不要继续跑 04（04 每岗约 5.5k 字符，是大头）。

    判据按稳到不稳：veto → 硬条件"不满足"占比 →（可选）关键词覆盖率。
    """
    from . import scoring

    veto = (jd or {}).get("veto") or {}
    if veto.get("blocked"):
        return False, "岗位明确不匹配（veto）：" + str(veto.get("reason", ""))[:80]

    reqs = (jd or {}).get("hard_requirements") or []
    fails = [r for r in reqs if str(r.get("status", "")) == "不满足"]
    if reqs and len(fails) / len(reqs) >= 0.5:
        names = "、".join(str(r.get("item", ""))[:16] for r in fails[:3])
        return False, f"硬条件 {len(fails)}/{len(reqs)} 条不满足（{names}）"

    if min_coverage > 0:
        cov = scoring.keyword_coverage_report(profile, jd)
        ratio = float(cov.get("coverage", 0) or 0)
        if ratio < min_coverage:
            return False, f"关键词覆盖 {ratio:.0%} 低于阈值 {min_coverage:.0%}"
    return True, ""
