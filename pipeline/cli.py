"""CLI 入口。

用法:
    python -m pipeline.cli run --resume <简历> --jd <JD> [--company <公司文本>]

密钥从本地 .env / 环境变量读取（见 llm.py），无需改代码。
"""
from __future__ import annotations

import argparse
import json
import sys

from . import ingest, steps


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline", description="求职投递建议流水线")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="单份简历 + 单个 JD 跑一遍")
    p_run.add_argument("--resume", required=True, help="简历文件（PDF / 图片 / txt）")
    p_run.add_argument("--jd", required=True, help="岗位描述（JD）文件")
    p_run.add_argument("--company", required=False, help="公司官网 about/价值观页文本（可选）")
    p_run.set_defaults(func=_run)
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
