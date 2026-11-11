#!/usr/bin/env python3
"""提交前密钥扫描：防止把真实 API key / 私钥误提交进公开仓库。

用法（在仓库根目录执行）：
    python scripts/scan_secrets.py             # 扫"将被提交的文件"（暂存区 + 已跟踪文件）
    python scripts/scan_secrets.py --staged    # 只扫已 git add 的暂存区
    python scripts/scan_secrets.py --history   # 扫全部提交历史（含已删除的文件）
    python scripts/scan_secrets.py --all       # 两者都扫

退出码：0 = 干净；1 = 发现疑似密钥（禁止提交）；2 = 环境错误（不是 git 仓库等）。

设计原则：
- 只做"发现并提醒"，不做自动改写，避免误删正常代码。
- 输出里的密钥一律脱敏（只留前 6 位 + 总长度），防止扫描日志本身又成为泄露渠道。
- 占位符（your_key / xxx / 尖括号 / 省略号）不算命中，避免 README 示例误报。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (名称, 正则)
PATTERNS = [
    ("OpenAI/DeepSeek 风格密钥", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("GitHub 个人访问令牌", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("GitHub 细粒度令牌", re.compile(r"github_pat_[A-Za-z0-9_]{40,}")),
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Slack Token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("私钥文件内容", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("飞书/企业微信 App Secret 赋值",
     re.compile(r"(?:app[_-]?secret|corp[_-]?secret)\s*[:=]\s*['\"]?[A-Za-z0-9]{20,}['\"]?", re.I)),
]

# 通用赋值型（容易误报，单独处理：必须不是占位符）
GENERIC = re.compile(
    r"(?:api[_-]?key|apikey|access[_-]?token|secret[_-]?key)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{24,})['\"]?",
    re.I,
)
PLACEHOLDER_HINTS = ("your", "xxx", "example", "placeholder", "changeme", "redacted", "here", "todo")

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip", ".gz", ".7z",
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".pyc", ".so", ".dll", ".exe", ".onnx", ".woff", ".woff2",
}
SKIP_DIR_PARTS = {".git", "node_modules", "__pycache__", "data", "legacy"}
MAX_BYTES = 2 * 1024 * 1024


def _mask(value: str) -> str:
    v = value.strip().strip("'\"")
    if len(v) <= 10:
        return "*" * len(v)
    return f"{v[:6]}…（共 {len(v)} 字符，已脱敏）"


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} 失败")
    return out.stdout


def is_repo() -> bool:
    try:
        _git("rev-parse", "--git-dir")
        return True
    except Exception:
        return False


def scan_text(text: str) -> list[tuple[str, str]]:
    """返回 [(规则名, 命中值)]。"""
    hits: list[tuple[str, str]] = []
    for name, rx in PATTERNS:
        for m in rx.finditer(text):
            hits.append((name, m.group(0)))
    for m in GENERIC.finditer(text):
        val = m.group(1)
        low = val.lower()
        if any(h in low for h in PLACEHOLDER_HINTS):
            continue
        if val.isdigit():
            continue
        hits.append(("通用 api_key/secret 赋值", val))
    return hits


def scan_files(paths: list[str], label: str) -> list[str]:
    problems: list[str] = []
    for rel in paths:
        p = REPO / rel
        if not p.is_file():
            continue
        if p.suffix.lower() in BINARY_EXT:
            continue
        parts = set(Path(rel).parts)
        if parts & SKIP_DIR_PARTS:
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, val in scan_text(line):
                problems.append(f"[命中] {label} {rel}:{lineno}  {name}  {_mask(val)}")
    return problems


def staged_files() -> list[str]:
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return [x for x in out.splitlines() if x.strip()]


def tracked_files() -> list[str]:
    out = _git("ls-files")
    return [x for x in out.splitlines() if x.strip()]


def history_scan() -> list[str]:
    """对每个历史提交做一次 git grep；命中的是"曾经进入过仓库"的内容。"""
    commits = [c for c in _git("rev-list", "--all").splitlines() if c.strip()]
    if not commits:
        return []
    combined = "|".join(rx.pattern for _, rx in PATTERNS)
    problems: list[str] = []
    batch = 100
    for i in range(0, len(commits), batch):
        chunk = commits[i : i + batch]
        try:
            out = subprocess.run(
                ["git", "grep", "-n", "-I", "-E", combined, *chunk],
                cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        except OSError:
            continue
        for line in out.stdout.splitlines():
            # 形如 commit:path:lineno:content
            seg = line.split(":", 3)
            if len(seg) < 4:
                continue
            commit, rel, lineno, content = seg
            for name, val in scan_text(content):
                problems.append(f"[历史命中] {commit[:8]} {rel}:{lineno}  {name}  {_mask(val)}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="提交前密钥扫描")
    ap.add_argument("--staged", action="store_true", help="只扫暂存区")
    ap.add_argument("--history", action="store_true", help="只扫提交历史")
    ap.add_argument("--all", action="store_true", help="暂存区 + 历史都扫")
    args = ap.parse_args(argv)

    if not is_repo():
        print(f"错误：{REPO} 不是 git 仓库", file=sys.stderr)
        return 2

    problems: list[str] = []
    if args.history or args.all:
        problems += history_scan()
    if not args.history:
        scope = staged_files() if args.staged else tracked_files()
        label = "暂存区" if args.staged else "已跟踪"
        problems += scan_files(scope, label)

    # 去重（同一处可能被多条规则命中）
    seen, uniq = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            uniq.append(p)

    for p in uniq:
        print(p)
    if uniq:
        print(f"\n发现 {len(uniq)} 处疑似密钥/私钥。请删除或改成占位符后重新 git add。")
        print("提示：本地 .env 不会被提交（.gitignore 已忽略 .env 与 .env.*），无需删除文件本身。")
        return 1
    print("[CLEAN] 未发现疑似密钥")
    return 0


if __name__ == "__main__":
    sys.exit(main())
