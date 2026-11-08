"""批量：读 jobs.jsonl，循环跑 run_pipeline，逐条落 results.jsonl。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import ingest, steps


_PATH_EXT = (".txt", ".md", ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif")


def load_jobs(path: str | Path) -> list[dict]:
    """读取 jobs.jsonl：每行一个岗位 JSON。

    jd / company 用 `file:` 前缀表示文件路径，否则视为纯文本。
    title 为岗位名（可选，透传给 run_pipeline）。
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


def run_batch(resume_text: str, jobs: list[tuple[int, dict]], *, run_dir=None, jobs_path=None) -> dict:
    """循环跑所有岗位；单个失败记录到结果并逐条落盘，不中断整体。

    jobs 为 [(原始行号, job), ...]：原始行号写进 rec["index"]，
    使 --retry-failed 能回溯到 jobs.jsonl 的真实行，而不是子集序号。
    """
    if run_dir is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        run_dir = steps.DATA_DIR / "runs" / ts
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    if jobs_path:
        (run_dir / "meta.json").write_text(
            json.dumps({"jobs_path": str(jobs_path)}, ensure_ascii=False), encoding="utf-8"
        )

    results_file = run_dir / "results.jsonl"
    results_file.write_text("", encoding="utf-8")  # 清空旧文件

    results = []
    for i, job in jobs:
        job_dir = run_dir / f"job-{i:02d}"
        try:
            r = steps.run_pipeline(
                resume_text,
                job["jd"],
                job.get("company", ""),
                run_dir=job_dir,
                position=job.get("title"),
            )
            rec = {"index": i, "ok": True, "advice": r["advice"], "coverage": r["coverage"]}
        except Exception as e:  # noqa: BLE001 记录任何异常（含网络），不整批炸掉
            rec = {
                "index": i,
                "ok": False,
                "title": job.get("title", ""),
                "step": getattr(e, "step", None),
                "error_type": type(e).__name__,
                "error": str(e),
            }
        results.append(rec)
        with results_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return {"run_dir": str(run_dir), "results": results}


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
