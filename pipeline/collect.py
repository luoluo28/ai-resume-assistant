"""企业官网 careers 页采集（仅公开页面，无头 Chromium）。"""
from __future__ import annotations

import sys

from . import guard


def fetch_page_text(url: str, *, timeout_ms: int = 30000) -> str:
    """抓取公开页面的可见文本，并做完整性守卫（清不可见字符、扫注入）。

    仅用于公开页面；勿抓需登录、或目标站点协议禁止的页面。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "采集需要 playwright，请先 pip install playwright && playwright install chromium"
        ) from e
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, timeout=timeout_ms)
        text = page.inner_text("body")
        browser.close()

    text, findings = guard.guard(text)
    if findings["verdict"] != "guard: clean":
        print(f"[告警] {url}: {findings['verdict']}", file=sys.stderr)
    return text.strip()
