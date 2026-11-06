"""CLI 入口。

用法:
    python -m pipeline.cli run --resume <简历> --jd <JD> [--company <公司文本>]
    python -m pipeline.cli batch --resume <简历> --jobs <jobs.jsonl>

密钥从本地 .env / 环境变量读取（见 llm.py），无需改代码。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import batch, export, ingest, steps


def _run(args: argparse.Namespace) -> int:
    resume_text = ingest.ingest(args.resume)
    jd_text = ingest.ingest(args.jd)
    company_text = ingest.ingest(args.company) if args.company else ""
    result = steps.run_pipeline(resume_text, jd_text, company_text)
    print(steps.render_advice(result["advice"]))
    print("\n--- 结构化 JSON ---")
    print(json.dumps(result["advice"], ensure_ascii=False, indent=2))
    print("\n中间产物目录:", result["run_dir"])
    return 0


def _batch(args: argparse.Namespace) -> int:
    resume_text = ingest.ingest(args.resume)
    jobs = batch.load_jobs(args.jobs)
    out = batch.run_batch(resume_text, jobs)
    results = out["results"]
    ok = sum(1 for r in results if r["ok"])
    print(f"完成 {ok}/{len(results)} 个岗位")
    for r in results:
        if r["ok"]:
            a = r["advice"]
            print(f"  [OK] {a.get('position', '')} 结论={a.get('conclusion', '')} 分数={a.get('score', '')}")
        else:
            print(f"  [失败] step={r.get('step', '')} {r.get('error', '')}")
    advices = [r["advice"] for r in results if r["ok"]]
    if advices:
        xlsx = Path(out["run_dir"]) / "summary.xlsx"
        export.to_excel(advices, xlsx)
        print("汇总表:", xlsx)
    print("results.jsonl:", Path(out["run_dir"]) / "results.jsonl")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline", description="求职投递建议流水线")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="单份简历 + 单个 JD 跑一遍")
    p_run.add_argument("--resume", required=True, help="简历文件（PDF / 图片 / txt）")
    p_run.add_argument("--jd", required=True, help="岗位描述（JD）文件")
    p_run.add_argument("--company", required=False, help="公司官网 about/价值观页文本（可选）")
    p_run.set_defaults(func=_run)

    p_batch = sub.add_parser("batch", help="批量跑多个岗位（一份简历对多个 JD）")
    p_batch.add_argument("--jobs", required=True, help="jobs.jsonl 路径（每行一个岗位 JSON）")
    p_batch.add_argument("--resume", required=True, help="简历文件（PDF / 图片 / txt）")
    p_batch.set_defaults(func=_batch)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (steps.StepError, ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
