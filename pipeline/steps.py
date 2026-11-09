"""流水线四步：profile_extract → jd_analysis → company_values → apply_advice。

每一步是纯函数、可单独运行；run_pipeline 串联四步并每步落中间产物到
data/runs/<timestamp>/。任一步失败抛 StepError（带步骤名），不静默跳过。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from . import ledger, llm, schema, scoring

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class StepError(Exception):
    """某一步失败时抛出，携带步骤名与详情，便于定位。"""

    def __init__(self, step: str, detail: str):
        super().__init__(f"步骤 {step} 失败: {detail}")
        self.step = step
        self.detail = detail


def _load_prompt(name: str) -> str:
    """读取 prompt 文件，去掉文档头（第一个 --- 之前），只留 prompt 正文。"""
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    if "---" in text:
        text = text.split("---", 1)[1]
    return text.strip()


def _fill(template: str, **kw: str) -> str:
    for k, v in kw.items():
        template = template.replace("{{" + k + "}}", v)
    return template


def _ensure_no_error(result: dict, step: str) -> dict:
    if isinstance(result, dict) and "error" in result:
        detail = str(result.get("error")) or ""
        raw = str(result.get("raw", "") or "")
        if raw:
            detail = f"{detail}（返回内容前 200 字：{raw[:200]}）"
        raise StepError(step, detail)
    return result


_FORBIDDEN_POS_WORDS = ("岗位职责", "任职要求", "工作内容", "任职资格", "岗位描述")
_POS_LEADING_RE = re.compile(r"^[【\[（(][^】\]）)]*[】\]）)]\s*|^(?:岗位|职位)\s*[:：]\s*")


def _position_from_jd(jd_text: str) -> str | None:
    """从 JD 原文机械提取岗位名：首个非空行、剥前缀、≤30 字、不含禁用词。"""
    for line in (jd_text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        s = _POS_LEADING_RE.sub("", s).strip()
        if not s:
            continue
        if len(s) > 30 or any(w in s for w in _FORBIDDEN_POS_WORDS):
            return None
        return s
    return None


def _cache_get(key: str, subdir: str) -> dict | None:
    f = DATA_DIR / "cache" / subdir / f"{key}.json"
    if f.exists():
        print(f"[cache] 命中 {subdir} {key[:8]}", file=sys.stderr)
        llm.note_cache_hit()
        return json.loads(f.read_text(encoding="utf-8"))
    return None


def _cache_put(key: str, subdir: str, obj: dict) -> None:
    f = DATA_DIR / "cache" / subdir / f"{key}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _mkey(text: str) -> str:
    """缓存 key 里带上模型名：换模型后旧缓存自动失效，不会出现"换了模型却还是旧结果"。"""
    return hashlib.sha256((text + "\x00model=" + llm.MODEL).encode("utf-8")).hexdigest()[:16]


# ---------------- 输入裁剪（控上下文长度，省钱） ----------------
JD_MAX_CHARS = 2000
_JD_KEEP_HINTS = ("要求", "职责", "熟悉", "掌握", "了解", "学历", "年限", "经验")


def trim_jd(text: str, limit: int = JD_MAX_CHARS) -> str:
    """JD 过长时裁剪：优先保留「任职要求/岗位职责」这类含硬条件的段落。

    ≤ limit 原样返回；否则按行挑含关键词的行，仍超长再按字符截断。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    kept, total = [], 0
    for ln in lines:
        if any(h in ln for h in _JD_KEEP_HINTS):
            kept.append(ln)
            total += len(ln)
            if total >= limit:
                break
    head = text[: max(0, limit - 200)].strip()
    body = "\n".join(kept)[: limit - len(head) - 10] if kept else ""
    result = (head + ("\n\n[以下为抽出的硬条件段落]\n" + body if body else "")).strip()
    return result[:limit]


def profile_lite(profile: dict, skills_max: int = 150, projects_max: int = 300) -> dict:
    """批量分析用的精简画像：只留能撑匹配判断的部分，避免每个岗位重复塞满。"""
    p = dict(profile or {})
    for k, lim in (("skills", skills_max), ("projects", projects_max), ("experience", 200)):
        v = str(p.get(k, "") or "")
        p[k] = (v[:lim] + "…") if len(v) > lim else v
    return p


def slim_jd_analysis(jd: dict, max_req: int = 8, max_kw: int = 12) -> dict:
    """把 02 的输出压成 04 真正需要的部分（2546 字符 → 约 800）。"""
    jd = jd or {}
    reqs = jd.get("hard_requirements") or []
    keywords = jd.get("keywords_weighted") or []
    return {
        "hard_requirements": reqs[:max_req],
        "keywords_weighted": keywords[:max_kw],
        "veto": jd.get("veto") or {"blocked": False, "reason": ""},
        "jd_summary": str(jd.get("jd_summary", "") or "")[:300],
    }


def profile_extract(resume_text: str) -> dict:
    key = _mkey(resume_text)
    cached = _cache_get(key, "profile")
    if cached is not None:
        return cached
    system = _fill(_load_prompt("01_profile_extract.md"), RESUME_TEXT=resume_text)
    data = llm.chat_json(system=system, user="", temperature=0.1, max_tokens=800, step="01_profile_extract")
    if "error" in data:
        return data
    result = schema.Profile.from_dict(data).to_dict()
    _cache_put(key, "profile", result)
    return result


def jd_analysis(jd_text: str, profile: dict) -> dict:
    jd_text = trim_jd(jd_text)
    key = _mkey(jd_text + "\x00" + json.dumps(profile, ensure_ascii=False, sort_keys=True))
    cached = _cache_get(key, "jd")
    if cached is not None:
        return cached

    system = _fill(
        _load_prompt("02_jd_analysis.md"),
        JD_TEXT=jd_text,
        PROFILE_JSON=json.dumps(profile, ensure_ascii=False),
    )
    data = llm.chat_json(system=system, user="", temperature=0, max_tokens=1400, step="02_jd_analysis")
    if "error" in data:
        return data
    result = schema.JdAnalysis.from_dict(data).to_dict()
    _cache_put(key, "jd", result)
    return result


def company_values(company_text: str, jd_text: str) -> dict:
    """第 3 步。公司文本为空时直接返回占位结果，不调用模型（省一次调用）。"""
    if not (company_text or "").strip():
        print("[skip] 未提供公司文本，第 3 步不调用模型", file=sys.stderr)
        return {"need_company_info": True, "labels": [], "hire_profile": ""}

    key = _mkey(company_text + "\x00" + jd_text + "\x00v1")
    cached = _cache_get(key, "company")
    if cached is not None:
        return cached

    system = _fill(
        _load_prompt("03_company_values.md"),
        COMPANY_TEXT=company_text,
        JD_TEXT=trim_jd(jd_text),
    )
    data = llm.chat_json(system=system, user="", temperature=0.1, max_tokens=600, step="03_company_values")
    if "error" in data:
        # 公司价值观只是加分项，解析失败不该让整个岗位作废：降级为占位，继续走第 4 步
        print(
            f"[warn] 第 3 步公司文本解析失败（{data.get('error')}），降级为占位继续",
            file=sys.stderr,
        )
        return {
            "need_company_info": True,
            "labels": [],
            "hire_profile": "",
            "note": "公司信息解析失败，已跳过这一步",
        }
    result = schema.CompanyValues.from_dict(data).to_dict()
    if len(result.get("labels", [])) > 5:
        print(f"[告警] company_values 返回 {len(result['labels'])} 个标签，已截断为 5", file=sys.stderr)
        result["labels"] = result["labels"][:5]
    _cache_put(key, "company", result)
    return result


def apply_advice(profile: dict, jd: dict, values: dict, *, position: str = "", lite: bool = False) -> dict:
    """第 4 步。lite=True 时用精简输入（省约 20% 字符，但会削掉软实力与部分项目细节，默认关闭）。"""
    p_in = profile_lite(profile) if lite else profile
    j_in = slim_jd_analysis(jd) if lite else jd
    key = _mkey(
        json.dumps(
            {"p": p_in, "j": j_in, "v": values, "pos": position, "v3": "advice-v4"},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    cached = _cache_get(key, "advice")
    if cached is not None:
        return cached

    system = _fill(
        _load_prompt("04_apply_advice.md"),
        PROFILE_JSON=json.dumps(p_in, ensure_ascii=False),
        JD_ANALYSIS_JSON=json.dumps(j_in, ensure_ascii=False),
        COMPANY_VALUES_JSON=json.dumps(values, ensure_ascii=False),
        POSITION=position,
    )
    data = llm.chat_json(system=system, user="", temperature=0.1, max_tokens=1500, step="04_apply_advice")
    if "error" in data:
        return data
    result = schema.ApplyAdvice.from_dict(data).to_dict()
    result["score"] = scoring.weighted_score(result["scores"])
    result["verdict"] = scoring.verdict_band(result["score"])
    if result.get("location_verdict") == "FAIL":
        result["conclusion"] = "不建议投"
        result["note"] = (result.get("note", "") + " 地点/通勤不满足，一票否决。").strip()
    if position:
        result["position"] = position  # 输入侧元数据覆盖，防止模型改写
    _cache_put(key, "advice", result)
    return result


def render_advice(advice: dict) -> str:
    """把 apply_advice 的 JSON 渲染成五段人话。"""
    head = advice.get("conclusion", "")
    parts = []
    if advice.get("verdict"):
        parts.append(str(advice["verdict"]))
    if advice.get("score"):
        parts.append(f"匹配度 {advice['score']}%")
    if parts:
        head += "（" + " · ".join(parts) + "）"
    lines = [f"1. 结论：{head}", "2. 理由："]
    for i, r in enumerate(advice.get("reasons", []), 1):
        lines.append(f"   {i}. {r}")
    lines.append("3. 差什么：")
    for g in advice.get("gaps", []):
        lines.append(f"   - {g}")
    lines.append(f"4. 怎么补：{advice.get('advice', '')}")
    lines.append(f"5. 打招呼第一句：{advice.get('opening_line', '')}")
    return "\n".join(lines)


def _new_run_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    d = DATA_DIR / "runs" / ts
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dump(run_dir: Path, name: str, obj: dict) -> None:
    (run_dir / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_pipeline(resume_text: str, jd_text: str, company_text: str = "", *, run_dir=None, position=None, on_step=None) -> dict:
    """串联四步；每步落中间文件；任一步失败抛 StepError（不静默跳过）。

    on_step(n: int, name: str) 可选回调，用于上报进度（如 Web 服务轮询）。
    """
    run_dir = Path(run_dir) if run_dir else _new_run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)

    def _step(n: int, name: str) -> None:
        if on_step:
            on_step(n, name)

    position = position or _position_from_jd(jd_text) or "未提供岗位名"

    # 原文落盘：让后续步骤（简历顾问）能复用同一 run，不必重新调用前四步
    (run_dir / "00_resume.txt").write_text(resume_text, encoding="utf-8")
    (run_dir / "00_jd.txt").write_text(jd_text, encoding="utf-8")

    _step(1, "profile_extract")
    profile = profile_extract(resume_text)
    _dump(run_dir, "01_profile.json", profile)
    _ensure_no_error(profile, "profile_extract")

    _step(2, "jd_analysis")
    jd = jd_analysis(jd_text, profile)
    _dump(run_dir, "02_jd_analysis.json", jd)
    _ensure_no_error(jd, "jd_analysis")

    _step(3, "company_values")
    values = company_values(company_text, jd_text)
    _dump(run_dir, "03_company_values.json", values)
    _ensure_no_error(values, "company_values")

    _step(4, "apply_advice")
    advice = apply_advice(profile, jd, values, position=position)
    _dump(run_dir, "04_apply_advice.json", advice)
    _ensure_no_error(advice, "apply_advice")

    if position == "未提供岗位名":
        advice["note"] = (advice.get("note", "") + " 岗位名未提供，按占位处理。").strip()

    cov = scoring.keyword_coverage_report(profile, jd)
    _dump(run_dir, "05_coverage.json", cov)

    ledger.upsert(jd_text, advice, str(run_dir))

    return {"run_dir": str(run_dir), "advice": advice, "coverage": cov}


if __name__ == "__main__":
    from . import ingest

    if len(sys.argv) not in (3, 4):
        print("用法: python -m pipeline.steps <简历文件> <JD文件> [公司官网文件]")
        sys.exit(2)
    resume_text = ingest.ingest(sys.argv[1])
    jd_text = ingest.ingest(sys.argv[2])
    company_text = ingest.ingest(sys.argv[3]) if len(sys.argv) == 4 else ""
    result = run_pipeline(resume_text, jd_text, company_text)
    print(render_advice(result["advice"]))
    print("\n--- 结构化 JSON ---")
    print(json.dumps(result["advice"], ensure_ascii=False, indent=2))
    print("\n中间产物目录:", result["run_dir"])
