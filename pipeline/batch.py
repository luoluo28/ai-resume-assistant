"""批量：读 jobs.jsonl，循环跑 run_pipeline，出 results.jsonl。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import ingest, steps


def load_jobs(path: str | Path) -> list[dict]:
    """读取 jobs.jsonl：每行一个岗位 JSON，jd/company 可为文件路径或纯文本。"""
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
    p = Path(text_or_path)
    if p.exists():
        return ingest.ingest(str(p))
    return text_or_path


def run_batch(resume_text: str, jobs: list[dict], *, run_dir=None) -> dict:
    """循环跑所有岗位；单个失败记录到结果但不中断整体。"""
    if run_dir is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = steps.DATA_DIR / "runs" / ts
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, job in enumerate(jobs):
        job_dir = run_dir / f"job-{i:02d}"
        try:
            r = steps.run_pipeline(
                resume_text, job["jd"], job.get("company", ""), run_dir=job_dir
            )
            results.append({"index": i, "ok": True, "advice": r["advice"], "scoring": r["scoring"]})
        except steps.StepError as e:
            results.append({"index": i, "ok": False, "step": e.step, "error": str(e)})

    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n",
        encoding="utf-8",
    )
    return {"run_dir": str(run_dir), "results": results}
