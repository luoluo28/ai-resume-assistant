"""批量：读 jobs.jsonl，机械预筛 → 两段漏斗 → 逐条落 results.jsonl。

省钱三道闸（详见 pipeline/prefilter.py）：
1. 预筛（0 调用）：去重 + 城市/学历/年限否决 + 技能覆盖率打分，只放行过线岗位；
2. 两段漏斗：放行的岗位先只跑 02（jd_analysis，便宜），veto 或覆盖率不达标就停，不跑 04；
3. 预算闸门：--budget-calls N，累计真实调用到 N 次即停止，剩余标「未分析-超预算」。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import ingest, llm, prefilter, scoring, steps


_PATH_EXT = (".txt", ".md", ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif")


def load_jobs(path: str | Path) -> list[dict]:
    """读取 jobs.jsonl：每行一个岗位 JSON。

    jd / company 用 `file:` 前缀表示文件路径，否则视为纯文本。
    title 为岗位名（可选），city 为城市（可选，用于预筛）。
    若字段「看起来像文件路径」却缺 file: 前缀，直接抛错（避免静默把路径当 JD 文本分析）。
    """
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as e:
        raise ValueError(f"岗位文件编码有问题（应为 UTF-8）：{e}") from e
    jobs = []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        obj["jd"] = _field(obj.get("jd", ""), "jd", i)
        company = obj.get("company", "")
        obj["company"] = _field(company, "company", i) if company else ""
        jobs.append(obj)
    return jobs


def _field(raw: str, name: str, lineno: int) -> str:
    if _looks_like_path(raw) and not raw.startswith("file:"):
        raise ValueError(
            f"第 {lineno} 行: {name} 看起来是文件路径但缺少 file: 前缀，请写成 \"file:<路径>\""
        )
    return _resolve(raw)


def _looks_like_path(s: str) -> bool:
    s = s.strip()
    if not s or "\n" in s:
        return False
    if s.lower().endswith(_PATH_EXT):
        return True  # 以已知扩展名结尾：强信号，不看长度
    return ("\\" in s or "/" in s) and " " not in s and len(s) > 20


def _resolve(text_or_path: str) -> str:
    """`file:` 前缀 → 读文件；否则视为纯文本原样返回。"""
    if text_or_path.startswith("file:"):
        return ingest.ingest(text_or_path[5:])
    return text_or_path


def _dump(path: Path, name: str, obj: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_one(
    resume_text: str,
    profile: dict,
    job: dict,
    job_dir: Path,
    *,
    min_coverage: float,
    lite: bool,
) -> dict:
    """单个岗位：02 → 闸门 → 03/04。返回记录（不抛异常）。"""
    jd_text = job["jd"]
    position = job.get("title") or steps._position_from_jd(jd_text) or "未提供岗位名"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "00_resume.txt").write_text(resume_text, encoding="utf-8")
    (job_dir / "00_jd.txt").write_text(jd_text, encoding="utf-8")
    _dump(job_dir, "01_profile.json", profile)

    jd = steps.jd_analysis(jd_text, profile)
    _dump(job_dir, "02_jd_analysis.json", jd)
    steps._ensure_no_error(jd, "jd_analysis")

    cov = scoring.keyword_coverage_report(profile, jd)
    _dump(job_dir, "05_coverage.json", cov)

    ok, reason = prefilter.gate_after_jd(jd, profile, min_coverage=min_coverage)
    if not ok:
        return {
            "index": job["_index"],
            "ok": True,
            "skipped": True,
            "title": job.get("title", ""),
            "position": position,
            "reason": reason,
            "coverage": cov,
        }

    values = steps.company_values(job.get("company", ""), jd_text)
    _dump(job_dir, "03_company_values.json", values)

    advice = steps.apply_advice(profile, jd, values, position=position, lite=lite)
    _dump(job_dir, "04_apply_advice.json", advice)
    steps._ensure_no_error(advice, "apply_advice")
    if position == "未提供岗位名":
        advice["note"] = (advice.get("note", "") + " 岗位名未提供，按占位处理。").strip()

    return {
        "index": job["_index"],
        "ok": True,
        "skipped": False,
        "title": job.get("title", ""),
        "position": position,
        "advice": advice,
        "coverage": cov,
    }


def run_batch(
    resume_text: str,
    jobs: list[tuple[int, dict]],
    *,
    run_dir=None,
    jobs_path=None,
    city: str = "",
    min_score: int = 60,
    min_coverage: float = 0.45,
    budget_calls: int = 0,
    dry_run: bool = False,
    lite: bool = False,
    profile: dict | None = None,
) -> dict:
    """批量跑：预筛 → 两段漏斗 → 逐条落盘；单条失败不中断整批。"""
    if run_dir is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        run_dir = steps.DATA_DIR / "runs" / ts
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    if jobs_path:
        (run_dir / "meta.json").write_text(
            json.dumps({"jobs_path": str(jobs_path)}, ensure_ascii=False), encoding="utf-8"
        )

    llm.reset_stats()
    llm.set_call_limit(budget_calls)
    if profile is None:
        profile = steps.profile_extract(resume_text)
    steps._ensure_no_error(profile, "profile_extract")

    pre = prefilter.prefilter(jobs, profile, city=city, min_score=min_score, min_coverage=min_coverage)
    _dump(run_dir, "prefilter.json", pre)

    if dry_run:
        llm.set_call_limit(0)
        return {
            "run_dir": str(run_dir),
            "prefilter": pre,
            "results": [],
            "dry_run": True,
            "stats": llm.stats(),
        }

    by_index = {i: job for i, job in jobs}
    results = []
    results_file = run_dir / "results.jsonl"
    results_file.write_text("", encoding="utf-8")

    for rec in pre["pass"]:
        i = rec["index"]
        job = dict(by_index[i])
        job["_index"] = i

        if budget_calls and llm.budget_left() is not None and llm.budget_left() < 1:
            results.append(
                {
                    "index": i,
                    "ok": False,
                    "skipped": True,
                    "budget_stop": True,
                    "title": job.get("title", ""),
                    "reason": f"未分析-超预算（已用 {llm.stats()['calls']}/{budget_calls} 次调用，剩不足一个岗位）",
                }
            )
            continue

        job_dir = run_dir / f"job-{i:02d}"
        try:
            results.append(
                _run_one(resume_text, profile, job, job_dir, min_coverage=min_coverage, lite=lite)
            )
        except llm.BudgetExceeded as e:  # 岗内调用越过上限：记录并停止后续
            results.append(
                {
                    "index": i,
                    "ok": False,
                    "skipped": True,
                    "budget_stop": True,
                    "title": job.get("title", ""),
                    "reason": f"未分析-超预算：{e}",
                }
            )
            for rest in pre["pass"][pre["pass"].index(rec) + 1 :]:
                results.append(
                    {
                        "index": rest["index"],
                        "ok": False,
                        "skipped": True,
                        "budget_stop": True,
                        "title": rest.get("title", ""),
                        "reason": f"未分析-超预算（已用 {llm.stats()['calls']}/{budget_calls} 次调用）",
                    }
                )
            break
        except Exception as e:  # noqa: BLE001 记录任何异常（含网络），不整批炸掉
            results.append(
                {
                    "index": i,
                    "ok": False,
                    "skipped": False,
                    "title": job.get("title", ""),
                    "step": getattr(e, "step", None),
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            )

    with results_file.open("a", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    llm.set_call_limit(0)
    return {
        "run_dir": str(run_dir),
        "prefilter": pre,
        "results": results,
        "dry_run": False,
        "stats": llm.stats(),
    }


def latest_results_file() -> Path | None:
    """最近一次 batch 的 results.jsonl。"""
    candidates = sorted((steps.DATA_DIR / "runs").glob("*/results.jsonl"))
    return candidates[-1] if candidates else None


def failed_indexes(results_file: Path) -> list[int]:
    idx = []
    for line in Path(results_file).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if not rec.get("ok"):
            idx.append(rec["index"])
    return idx


def read_jobs_path(run_dir) -> str | None:
    meta = Path(run_dir) / "meta.json"
    if meta.exists():
        try:
            return json.loads(meta.read_text(encoding="utf-8")).get("jobs_path")
        except (json.JSONDecodeError, OSError):
            return None
    return None
