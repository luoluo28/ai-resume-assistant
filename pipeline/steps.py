"""流水线四步：profile_extract → jd_analysis → company_values → apply_advice。

每一步是纯函数、可单独运行；run_pipeline 串联四步并每步落中间产物到
data/runs/<timestamp>/。任一步失败抛 StepError（带步骤名），不静默跳过。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from . import llm, schema, scoring

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
        detail = str(result.get("error")) or str(result.get("raw", ""))
        raise StepError(step, detail)
    return result


def profile_extract(resume_text: str) -> dict:
    system = _fill(_load_prompt("01_profile_extract.md"), RESUME_TEXT=resume_text)
    data = llm.chat_json(system=system, user="", temperature=0.1)
    if "error" in data:
        return data
    return schema.Profile.from_dict(data).to_dict()


def jd_analysis(jd_text: str, profile: dict) -> dict:
    system = _fill(
        _load_prompt("02_jd_analysis.md"),
        JD_TEXT=jd_text,
        PROFILE_JSON=json.dumps(profile, ensure_ascii=False),
    )
    data = llm.chat_json(system=system, user="", temperature=0.1)
    if "error" in data:
        return data
    return schema.JdAnalysis.from_dict(data).to_dict()


def company_values(company_text: str, jd_text: str) -> dict:
    system = _fill(
        _load_prompt("03_company_values.md"),
        COMPANY_TEXT=company_text,
        JD_TEXT=jd_text,
    )
    data = llm.chat_json(system=system, user="", temperature=0.1)
    if "error" in data:
        return data
    return schema.CompanyValues.from_dict(data).to_dict()


def apply_advice(profile: dict, jd: dict, values: dict) -> dict:
    system = _fill(
        _load_prompt("04_apply_advice.md"),
        PROFILE_JSON=json.dumps(profile, ensure_ascii=False),
        JD_ANALYSIS_JSON=json.dumps(jd, ensure_ascii=False),
        COMPANY_VALUES_JSON=json.dumps(values, ensure_ascii=False),
    )
    data = llm.chat_json(system=system, user="", temperature=0.1)
    if "error" in data:
        return data
    return schema.ApplyAdvice.from_dict(data).to_dict()


def render_advice(advice: dict) -> str:
    """把 apply_advice 的 JSON 渲染成五段人话。"""
    score = advice.get("score")
    score_part = f"（匹配度 {score}%）" if score not in (None, "", 0) else ""
    lines = [f"1. 结论：{advice.get('conclusion', '')}{score_part}", "2. 理由："]
    for i, r in enumerate(advice.get("reasons", []), 1):
        lines.append(f"   {i}. {r}")
    lines.append("3. 差什么：")
    for g in advice.get("gaps", []):
        lines.append(f"   - {g}")
    lines.append(f"4. 怎么补：{advice.get('advice', '')}")
    lines.append(f"5. 打招呼第一句：{advice.get('opening_line', '')}")
    return "\n".join(lines)


def _new_run_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = DATA_DIR / "runs" / ts
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dump(run_dir: Path, name: str, obj: dict) -> None:
    (run_dir / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_pipeline(resume_text: str, jd_text: str, company_text: str = "", *, run_dir=None) -> dict:
    """串联四步；每步落中间文件；任一步失败抛 StepError（不静默跳过）。"""
    run_dir = Path(run_dir) if run_dir else _new_run_dir()
    run_dir.mkdir(parents=True, exist_ok=True)

    profile = profile_extract(resume_text)
    _dump(run_dir, "01_profile.json", profile)
    _ensure_no_error(profile, "profile_extract")

    jd = jd_analysis(jd_text, profile)
    _dump(run_dir, "02_jd_analysis.json", jd)
    _ensure_no_error(jd, "jd_analysis")

    values = company_values(company_text, jd_text)
    _dump(run_dir, "03_company_values.json", values)
    _ensure_no_error(values, "company_values")

    advice = apply_advice(profile, jd, values)
    _dump(run_dir, "04_apply_advice.json", advice)
    _ensure_no_error(advice, "apply_advice")

    sc = scoring.compute(profile, jd, values)
    _dump(run_dir, "05_scoring.json", sc)

    return {"run_dir": str(run_dir), "advice": advice, "scoring": sc}


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
