"""批量：读 jobs.jsonl，循环跑 run_pipeline，逐条落 results.jsonl。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import ingest, steps


def load_jobs(path: str | Path) -> list[dict]:
    """读取 jobs.jsonl：每行一个岗位 JSON。

    jd / company 用 `file:` 前缀表示文件路径，否则视为纯文本。
    title 为岗位名（可选，透传给 run_pipeline）。
    """
    jobs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        obj["jd"] = _resolve(obj.get("jd", ""))
        company = obj.get("company", "")
        obj["company"] = _resolve(company) if company else ""
        jobs.append(obj)
    return jobs


def _resolve(text_or_path: str) -> str:
    """`file:` 前缀 → 读文件；否则视为纯文本原样返回。"""
    if text_or_path.startswith("file:"):
        return ingest.ingest(text_or_path[5:])
    return text_or_path


def run_batch(resume_text: str, jobs: list[dict], *, run_dir=None) -> dict:
    """循环跑所有岗位；单个失败记录到结果并逐条落盘，不中断整体。"""
    if run_dir is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        run_dir = steps.DATA_DIR / "runs" / ts
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    results_file = run_dir / "results.jsonl"
    results_file.write_text("", encoding="utf-8")  # 清空旧文件

    results = []
    for i, job in enumerate(jobs):
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
                "step": getattr(e, "step", None),
                "error_type": type(e).__name__,
                "error": str(e),
            }
        results.append(rec)
        with results_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return {"run_dir": str(run_dir), "results": results}
