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

from . import advisor, advisor_guard, batch, export, feishu, ingest, ledger, sources, steps


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


def _advisor(args: argparse.Namespace) -> int:
    """简历顾问：优先复用已有 run（只花 1 次调用），否则现跑前四步。"""
    if args.run:
        d = Path(args.run)
        resume_text = (d / "00_resume.txt").read_text(encoding="utf-8")
        profile = json.loads((d / "01_profile.json").read_text(encoding="utf-8"))
        jd = json.loads((d / "02_jd_analysis.json").read_text(encoding="utf-8"))
        advice = json.loads((d / "04_apply_advice.json").read_text(encoding="utf-8"))
        position = advice.get("position", "") or ""
    else:
        if not (args.resume and args.jd):
            print("需要 --run，或同时给 --resume 与 --jd", file=sys.stderr)
            return 2
        resume_text = ingest.ingest(args.resume)
        jd_text = ingest.ingest(args.jd)
        result = steps.run_pipeline(resume_text, jd_text, position=args.title)
        d = Path(result["run_dir"])
        profile = json.loads((d / "01_profile.json").read_text(encoding="utf-8"))
        jd = json.loads((d / "02_jd_analysis.json").read_text(encoding="utf-8"))
        advice = result["advice"]
        position = advice.get("position", "") or ""

    adv = advisor.resume_advisor(resume_text, profile, jd, advice, position=position)
    if "error" in adv:
        print(f"顾问生成失败: {adv['error']}", file=sys.stderr)
        return 1
    (d / "06_resume_advisor.json").write_text(
        json.dumps(adv, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = advisor_guard.render_markdown(adv)
    print(text)
    if args.md:
        Path(args.md).write_text(text, encoding="utf-8")
        print("\n已写入 Markdown:", args.md)
    print("\n顾问 JSON:", d / "06_resume_advisor.json")
    return 0


def _print_prefilter(pre: dict) -> None:
    p, d, dup = pre["pass"], pre["drop"], pre["dupes"]
    print(f"\n[预筛] 放行 {len(p)} / 否决 {len(d)} / 重复 {len(dup)}", file=sys.stderr)
    for r in p:
        print(
            f"  ✔ 第{r['index']}条 {r['title'] or '(无标题)'} {r['city']} "
            f"分数 {r['score']}（命中 {r['matched']}/{r['terms']} 项：{'、'.join(r['matched_terms'][:5])}）",
            file=sys.stderr,
        )
    for r in d:
        print(f"  ✘ 第{r['index']}条 {r['title'] or '(无标题)'} — {'；'.join(r['reasons'])}", file=sys.stderr)
    for r in dup:
        print(f"  = 第{r['index']}条 与第{r['same_as_index']}条重复", file=sys.stderr)


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
        jobs = [(i, all_jobs[i]) for i in failed if i < len(all_jobs)]
        print(f"本次只重跑 {len(jobs)} 条（上次 {len(all_jobs)} 个岗位中失败 {len(failed)} 条）")
    else:
        if not jobs_path:
            print("需要 --jobs 指定岗位文件", file=sys.stderr)
            return 2
        jobs = list(enumerate(batch.load_jobs(jobs_path)))
    if args.max_jobs and args.max_jobs > 0 and len(jobs) > args.max_jobs:
        total = len(jobs)
        jobs = jobs[: args.max_jobs]
        print(f"[limit] 本次只跑前 {args.max_jobs}/{total} 条（--max-jobs {args.max_jobs}），其余未处理", file=sys.stderr)

    out = batch.run_batch(
        resume_text,
        jobs,
        jobs_path=jobs_path,
        city=args.city or "",
        min_score=args.min_score,
        min_coverage=args.min_coverage,
        budget_calls=args.budget_calls,
        dry_run=args.dry_run,
    )
    _print_prefilter(out["prefilter"])

    if out["dry_run"]:
        print(f"\n[dry-run] 未调用模型。预筛表已写入: {Path(out['run_dir']) / 'prefilter.json'}")
        st = out.get("stats") or {}
        print(f"本次调用 {st.get('calls', 0)} 次（只含第 1 步简历解析，命中缓存 {st.get('cache_hits', 0)} 次）")
        return 0

    results = out["results"]
    done = [r for r in results if r.get("ok") and not r.get("skipped")]
    gated = [
        r for r in results
        if r.get("skipped") and not r.get("budget_stop") and "超预算" not in str(r.get("reason", ""))
    ]
    over = [r for r in results if r.get("budget_stop")]
    failed = [r for r in results if not r.get("ok") and not r.get("skipped")]

    print(f"\n完成 {len(done)}｜两段漏斗筛掉 {len(gated)}｜超预算未跑 {len(over)}｜失败 {len(failed)}")
    for r in done:
        a = r["advice"]
        print(f"  [OK] {a.get('position', '')} 结论={a.get('conclusion', '')} 分数={a.get('score', '')}")
    for r in gated:
        print(f"  [筛掉] {r.get('title', '')} — {r.get('reason', '')}")
    for r in over:
        print(f"  [未跑] {r.get('title', '')} — {r.get('reason', '')}")
    for r in failed:
        print(f"  [失败] {r.get('title', '')} step={r.get('step', '')} type={r.get('error_type', '')} {r.get('error', '')}")

    advices = [r["advice"] for r in done]
    if advices:
        xlsx = Path(out["run_dir"]) / "summary.xlsx"
        export.to_excel(advices, xlsx)
        print("汇总表:", xlsx)

    st = out.get("stats") or {}
    print(
        f"\n[用量] 调用 {st.get('calls', 0)} 次｜命中缓存 {st.get('cache_hits', 0)} 次｜"
        f"输入 {st.get('prompt_chars', 0)} 字符｜"
        f"prompt {st.get('prompt_tokens', 0)} tokens / completion {st.get('completion_tokens', 0)} tokens"
    )
    print("results.jsonl:", Path(out["run_dir"]) / "results.jsonl")
    print("预筛表:", Path(out["run_dir"]) / "prefilter.json")
    return 0


def _sources(args: argparse.Namespace) -> int:
    cats = [c.strip() for c in args.categories.replace("，", ",").split(",") if c.strip()]
    if not cats:
        print("请用 --categories 给出至少一个岗位类别，例如 --categories \"AI应用开发,Agent开发\"", file=sys.stderr)
        return 2
    path = sources.write_files(cats, city=args.city or "", out=args.out)
    print(f"城市：{args.city or '不限'}｜类别：{' / '.join(cats)}")
    for link in sources.build(cats, args.city or ""):
        print(f"  [{link['platform']}] {link['category']} → {link['url']}")
    print(f"\n已写入：{path}")
    print("下一步：点开链接挑岗位 → 把 JD 存进 岗位JD\\ → 登记 jobs.jsonl → batch --dry-run")
    return 0


def _collect(args: argparse.Namespace) -> int:
    try:
        r = sources.collect_dir(args.dir, out=args.out, city=args.city or "")
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    print(f"扫描目录：{args.dir}")
    print(f"收录 {r['count']} 个岗位 → {r['out']}")
    for row in r["rows"]:
        print(f"  · {row['title']}｜城市 {row['city'] or '(未识别)'}｜JD {len(row['jd'])} 字")
    for name, why in r["skipped"]:
        print(f"  [跳过] {name} — {why}")
    if not r["count"]:
        print("\n目录里没有可用的 JD 文本。先把岗位描述复制进 .txt 文件（每岗一个），再跑这条命令。")
        return 1
    print("\n下一步：先不花钱看筛不筛得动 →")
    print(
        f'  & "C:\\Users\\马\\.venv-resume-review\\Scripts\\python.exe" -m pipeline.cli batch '
        f'--resume "{args.resume or '简历.jpg'}" --jobs "{r["out"]}" --dry-run'
    )
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
        n = ledger.set_status(args.set, args.status, args.jd_hash)
        print(f"已更新 {n} 条：{args.set} → {args.status}")
        return 0
    entries = ledger.list_entries(args.status)
    if not entries:
        print("（无台账记录）")
        return 0
    for e in entries:
        updated = e.get("status_changed_at") or e.get("last_seen")
        print(
            f"{e['position']}  分数={e['score']}  {e['verdict']}  "
            f"地点={e['location_verdict']}  {e['status']}  hash={e.get('jd_hash', '')}  更新={updated}"
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

    p_advisor = sub.add_parser("advisor", help="简历顾问：对标 JD 优化 + 项目叙事 + 面试引导")
    p_advisor.add_argument("--run", help="已有 run 目录（复用 01/02/04 与 00_resume.txt，只花 1 次调用）")
    p_advisor.add_argument("--resume", help="简历文件（不给 --run 时必填）")
    p_advisor.add_argument("--jd", help="JD 文件（不给 --run 时必填）")
    p_advisor.add_argument("--title", help="岗位名（可选）")
    p_advisor.add_argument("--md", help="把结果另存为 Markdown 的路径（可选）")
    p_advisor.set_defaults(func=_advisor)

    p_batch = sub.add_parser("batch", help="批量跑多个岗位（一份简历对多个 JD）")
    p_batch.add_argument("--jobs", required=False, help="jobs.jsonl 路径（每行一个岗位 JSON；--retry-failed 时可省略）")
    p_batch.add_argument("--resume", required=True, help="简历文件（PDF / 图片 / txt）")
    p_batch.add_argument("--retry-failed", action="store_true", help="只重跑上次失败的岗位")
    p_batch.add_argument("--max-jobs", type=int, default=0, help="最多跑 N 条（0=不限）")
    p_batch.add_argument("--dry-run", action="store_true", help="只做机械预筛、不调用模型，输出预筛表")
    p_batch.add_argument("--budget-calls", type=int, default=0, help="本次最多花 N 次模型调用（0=不限）")
    p_batch.add_argument("--min-score", type=int, default=60, help="机械预筛分数阈值（0–100，低于则不进模型；0=关闭预筛）")
    p_batch.add_argument("--min-coverage", type=float, default=0.0, help="两段漏斗：额外要求关键词覆盖率≥N（默认 0=不用这条，靠 veto 和硬条件判断）")
    p_batch.add_argument("--city", default="", help="目标城市（用于预筛；不填则不按城市筛）")
    p_batch.set_defaults(func=_batch)

    p_sources = sub.add_parser("sources", help="按岗位类别+城市生成各平台搜索链接（不抓取、不登录、不投递）")
    p_sources.add_argument("--categories", required=True, help="岗位类别，逗号分隔，如 \"AI应用开发,Agent开发\"")
    p_sources.add_argument("--city", default="", help="城市，如 北京")
    p_sources.add_argument("--out", help="输出 Markdown 路径（默认 data/runs/<时间>/search_links.md）")
    p_sources.set_defaults(func=_sources)

    p_collect = sub.add_parser("collect", help="把「岗位JD」目录里的 .txt 汇总成 jobs.jsonl")
    p_collect.add_argument("--dir", required=True, help="存放岗位 txt 的目录，如 C:\\Users\\马\\简历助手\\岗位JD")
    p_collect.add_argument("--out", help="输出 jobs.jsonl 路径（默认写在该目录下）")
    p_collect.add_argument("--city", default="", help="文件名里没城市时用的默认城市")
    p_collect.add_argument("--resume", default="", help="仅用于末尾提示下一条命令的简历路径")
    p_collect.set_defaults(func=_collect)

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
    p_ledger.add_argument("--jd-hash", help="按 jd_hash 指定唯一条目（配合 --set，避免同名串改）")
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
