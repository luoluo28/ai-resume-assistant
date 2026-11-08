"""CLI 入口。

用法:
    python -m pipeline.cli run --resume <简历> --jd <JD> [--company <公司>] [--title <岗位名>]
    python -m pipeline.cli batch --resume <简历> --jobs <jobs.jsonl>
    python -m pipeline.cli export --advice <04_apply_advice.json> [--xlsx out.xlsx] [--pdf out.pdf]
    python -m pipeline.cli feishu --advice <04_apply_advice.json> --app-token <t> --table-id <t>

密钥从本地 .env / 环境变量读取（见 llm.py），无需改代码。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import batch, export, feishu, ingest, ledger, steps


def _run(args: argparse.Namespace) -> int:
    resume_text = ingest.ingest(args.resume)
    jd_text = ingest.ingest(args.jd)
    company_text = ingest.ingest(args.company) if args.company else ""
    result = steps.run_pipeline(resume_text, jd_text, company_text, position=args.title)
    print(steps.render_advice(result["advice"]))
    print("\n--- 结构化 JSON ---")
    print(json.dumps(result["advice"], ensure_ascii=False, indent=2))
    print("\n--- 关键词覆盖率 ---")
    print(json.dumps(result["coverage"], ensure_ascii=False, indent=2))
    print("\n中间产物目录:", result["run_dir"])
    return 0


def _batch(args: argparse.Namespace) -> int:
    resume_text = ingest.ingest(args.resume)
    jobs_path = args.jobs
    if args.retry_failed:
        rf = batch.latest_results_file()
        if rf is None:
            print("没有可重跑的失败记录", file=sys.stderr)
            return 1
        failed = batch.failed_indexes(rf)
        if not failed:
            print("上次没有失败的岗位")
            return 0
        if not jobs_path:
            jobs_path = batch.read_jobs_path(rf.parent)
        if not jobs_path:
            print("无法定位上次的 jobs 文件，请用 --jobs 指定", file=sys.stderr)
            return 1
        all_jobs = batch.load_jobs(jobs_path)
        jobs = [all_jobs[i] for i in failed if i < len(all_jobs)]
        print(f"本次只重跑 {len(jobs)} 条（上次 {len(all_jobs)} 个岗位中失败 {len(failed)} 条），预计最多 {len(jobs) * 4} 次调用")
    else:
        if not jobs_path:
            print("需要 --jobs 指定岗位文件", file=sys.stderr)
            return 2
        jobs = batch.load_jobs(jobs_path)
    out = batch.run_batch(resume_text, jobs, jobs_path=jobs_path)
    results = out["results"]
    ok = sum(1 for r in results if r["ok"])
    print(f"完成 {ok}/{len(results)} 个岗位")
    for r in results:
        if r["ok"]:
            a = r["advice"]
            print(f"  [OK] {a.get('position', '')} 结论={a.get('conclusion', '')} 分数={a.get('score', '')}")
        else:
            print(f"  [失败] step={r.get('step', '')} type={r.get('error_type', '')} {r.get('error', '')}")
    advices = [r["advice"] for r in results if r["ok"]]
    if advices:
        xlsx = Path(out["run_dir"]) / "summary.xlsx"
        export.to_excel(advices, xlsx)
        print("汇总表:", xlsx)
    print("results.jsonl:", Path(out["run_dir"]) / "results.jsonl")
    return 0


def _export(args: argparse.Namespace) -> int:
    advice = json.loads(Path(args.advice).read_text(encoding="utf-8"))
    if args.xlsx:
        export.to_excel([advice], args.xlsx)
    if args.pdf:
        export.to_pdf(advice, args.pdf)
    if not args.xlsx and not args.pdf:
        print("请至少指定 --xlsx 或 --pdf", file=sys.stderr)
        return 2
    print("已导出:", args.xlsx or "", args.pdf or "")
    return 0


def _feishu(args: argparse.Namespace) -> int:
    advice = json.loads(Path(args.advice).read_text(encoding="utf-8"))
    feishu.to_feishu([advice], args.app_token, args.table_id)
    print("已写入飞书多维表格")
    return 0


def _ledger(args: argparse.Namespace) -> int:
    if args.stats:
        for k, v in ledger.stats().items():
            print(f"{k}: {v}")
        return 0
    if args.set:
        if not args.status:
            print("--set 需要配合 --status", file=sys.stderr)
            return 2
        n = ledger.set_status(args.set, args.status)
        print(f"已更新 {n} 条：{args.set} → {args.status}")
        return 0
    entries = ledger.list_entries(args.status)
    if not entries:
        print("（无台账记录）")
        return 0
    for e in entries:
        print(
            f"{e['position']}  分数={e['score']}  {e['verdict']}  "
            f"地点={e['location_verdict']}  {e['status']}  更新={e['last_seen']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline", description="求职投递建议流水线")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="单份简历 + 单个 JD 跑一遍")
    p_run.add_argument("--resume", required=True, help="简历文件（PDF / 图片 / txt）")
    p_run.add_argument("--jd", required=True, help="岗位描述（JD）文件")
    p_run.add_argument("--company", required=False, help="公司官网 about/价值观页文本（可选）")
    p_run.add_argument("--title", required=False, help="岗位名（可选；不给则从 JD 首行机械提取）")
    p_run.set_defaults(func=_run)

    p_batch = sub.add_parser("batch", help="批量跑多个岗位（一份简历对多个 JD）")
    p_batch.add_argument("--jobs", required=False, help="jobs.jsonl 路径（每行一个岗位 JSON；--retry-failed 时可省略）")
    p_batch.add_argument("--resume", required=True, help="简历文件（PDF / 图片 / txt）")
    p_batch.add_argument("--retry-failed", action="store_true", help="只重跑上次失败的岗位")
    p_batch.set_defaults(func=_batch)

    p_export = sub.add_parser("export", help="把结果导出 Excel/PDF")
    p_export.add_argument("--advice", required=True, help="04_apply_advice.json 路径")
    p_export.add_argument("--xlsx", help="输出 xlsx 路径")
    p_export.add_argument("--pdf", help="输出 pdf 路径")
    p_export.set_defaults(func=_export)

    p_feishu = sub.add_parser("feishu", help="写入飞书多维表格")
    p_feishu.add_argument("--advice", required=True, help="04_apply_advice.json 路径")
    p_feishu.add_argument("--app-token", required=True, help="多维表格 app_token")
    p_feishu.add_argument("--table-id", required=True, help="数据表 table_id")
    p_feishu.set_defaults(func=_feishu)

    p_ledger = sub.add_parser("ledger", help="投递台账")
    p_ledger.add_argument("--list", action="store_true", help="列出台账（默认动作）")
    p_ledger.add_argument("--set", metavar="position", help="按岗位名改状态（配合 --status）")
    p_ledger.add_argument("--stats", action="store_true", help="各状态计数")
    p_ledger.add_argument("--status", choices=list(ledger.STATUSES), help="状态筛选 / 设置")
    p_ledger.set_defaults(func=_ledger)
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
