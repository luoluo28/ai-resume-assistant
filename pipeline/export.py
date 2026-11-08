"""导出交付：Excel（openpyxl）、PDF（HTML → Chromium/Playwright）。

PDF 中文不走 reportlab 直写，而是渲染成 HTML 后由无头 Chromium 输出，
中文字体由浏览器系统字体兜底（Microsoft YaHei / PingFang / SimSun）。
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

_HEADERS = ["岗位名", "结论", "分数", "理由", "差距", "建议", "打招呼"]


def advice_to_row(advice: dict) -> dict:
    return {
        "岗位名": advice.get("position", ""),
        "结论": advice.get("conclusion", ""),
        "分数": advice.get("score", ""),
        "理由": "\n".join(advice.get("reasons", [])),
        "差距": "\n".join(advice.get("gaps", [])),
        "建议": advice.get("advice", ""),
        "打招呼": advice.get("opening_line", ""),
    }


def to_excel(advices: list[dict], path: str | Path) -> None:
    """把多条投递建议写成 .xlsx（中文无乱码）。"""
    try:
        import openpyxl
    except ImportError as e:
        raise RuntimeError("导出 Excel 需要 openpyxl，请先 pip install openpyxl") from e
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "投递建议"
    ws.append(_HEADERS)
    for a in advices:
        row = advice_to_row(a)
        ws.append([row.get(h, "") for h in _HEADERS])
    wb.save(str(path))


def to_pdf(advice: dict, path: str | Path) -> None:
    """把单份投递建议渲染成 PDF（HTML → 无头 Chromium）。"""
    _html_to_pdf(advice_html(advice), str(path))


def advice_html(advice: dict) -> str:
    esc = lambda x: html.escape(str(x), quote=False)
    score = advice.get("score", "")
    score_s = f"（匹配度 {esc(score)}%）" if score not in (None, "", 0) else ""
    reasons = "".join(f"<li>{esc(r)}</li>" for r in advice.get("reasons", []))
    gaps = "".join(f"<li>{esc(g)}</li>" for g in advice.get("gaps", []))
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", "SimSun", sans-serif;
         font-size: 14px; line-height: 1.7; margin: 40px; }}
  h1 {{ font-size: 20px; }}
  .meta {{ color: #555; }}
  ul, ol {{ margin: 6px 0; }}
</style></head>
<body>
<h1>投递建议：{esc(advice.get('position', ''))}</h1>
<div class="meta">结论：{esc(advice.get('conclusion', ''))}{score_s}</div>
<h2>理由</h2><ol>{reasons}</ol>
<h2>差什么</h2><ul>{gaps}</ul>
<h2>怎么补</h2><p>{esc(advice.get('advice', ''))}</p>
<h2>打招呼第一句</h2><p>{esc(advice.get('opening_line', ''))}</p>
</body></html>"""


def _html_to_pdf(html: str, path: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "导出 PDF 需要 playwright，请先 pip install playwright && playwright install chromium"
        ) from e
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        page.pdf(path=path)
        browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("用法: python -m pipeline.export <advice.json> <out.xlsx> <out.pdf>")
        sys.exit(2)
    advice = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    to_excel([advice], sys.argv[2])
    to_pdf(advice, sys.argv[3])
    print("已导出:", sys.argv[2], "和", sys.argv[3])
