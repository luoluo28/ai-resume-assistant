"""导出交付：Excel（openpyxl）、PDF（HTML → Chromium/Playwright）。

PDF 中文不走 reportlab 直写，而是渲染成 HTML 后由无头 Chromium 输出，
中文字体由浏览器系统字体兜底（Microsoft YaHei / PingFang / SimSun）。
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

_HEADERS = ["岗位名", "结论", "分数", "分档", "四维", "四维依据", "地点判定", "地点说明", "理由", "差距", "建议", "打招呼"]


_DIM_SHORT = {"technical": "技", "experience": "经", "behavioral": "软", "career": "职"}
_DIM_LABELS = {"technical": "技术 ", "experience": "经验 ", "behavioral": "软实力 ", "career": "职业 "}


def _dims_str(scores: dict, labels: dict) -> str:
    if not scores:
        return ""
    return " · ".join(
        f"{labels[k]}{v}" for k, v in scores.items()
        if k in labels and isinstance(v, (int, float))
    )


def advice_to_row(advice: dict) -> dict:
    sr = advice.get("score_reasons") or {}
    dim_cn = [("technical", "技术"), ("experience", "经验"), ("behavioral", "软实力"), ("career", "职业")]
    sr_text = "\n".join(f"{cn}：{sr[k]}" for k, cn in dim_cn if sr.get(k))
    return {
        "岗位名": advice.get("position", ""),
        "结论": advice.get("conclusion", ""),
        "分数": advice.get("score", ""),
        "分档": advice.get("verdict", ""),
        "四维": _dims_str(advice.get("scores") or {}, _DIM_SHORT),
        "四维依据": sr_text,
        "地点判定": advice.get("location_verdict", ""),
        "地点说明": advice.get("location_note", ""),
        "理由": "\n".join(advice.get("reasons", [])),
        "差距": "\n".join(advice.get("gaps", [])),
        "建议": advice.get("advice", ""),
        "打招呼": advice.get("opening_line", ""),
    }


_COL_WIDTHS = {
    "岗位名": 24, "结论": 16, "分数": 8, "分档": 12, "四维": 28,
    "四维依据": 56, "地点判定": 10, "地点说明": 34,
    "理由": 62, "差距": 42, "建议": 42, "打招呼": 32,
}
_CENTER_COLS = {"分数", "分档", "四维", "地点判定"}


def to_excel(advices: list[dict], path: str | Path) -> None:
    """把多条投递建议写成 .xlsx（中文无乱码、有列宽、自动换行）。"""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError("导出 Excel 需要 openpyxl，请先 pip install openpyxl") from e

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "投递建议"
    ws.append(_HEADERS)

    head_fill = PatternFill("solid", fgColor="0F766E")
    for i, h in enumerate(_HEADERS, 1):
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = _COL_WIDTHS.get(h, 16)
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"

    for a in advices:
        row = advice_to_row(a)
        r = ws.max_row + 1
        for i, h in enumerate(_HEADERS, 1):
            cell = ws.cell(row=r, column=i, value=row.get(h, ""))
            cell.alignment = Alignment(
                wrap_text=True,
                vertical="top",
                horizontal="center" if h in _CENTER_COLS else "left",
            )
    wb.save(str(path))


def to_pdf(advice: dict, path: str | Path) -> None:
    """把单份投递建议渲染成 PDF（HTML → 无头 Chromium）。"""
    _html_to_pdf(advice_html(advice), str(path))


def advice_html(advice: dict) -> str:
    esc = lambda x: html.escape(str(x), quote=False)
    score = advice.get("score", "")
    verdict = esc(advice.get("verdict", ""))
    loc = esc(advice.get("location_verdict", ""))
    dims = esc(_dims_str(advice.get("scores") or {}, _DIM_LABELS))
    reasons = "".join(f"<li>{esc(r)}</li>" for r in advice.get("reasons", []))
    gaps = "".join(f"<li>{esc(g)}</li>" for g in advice.get("gaps", []))

    meta = " ｜ ".join(p for p in [
        f"结论：{esc(advice.get('conclusion', ''))}",
        f"分数：{esc(score)}" if score not in (None, "", 0) else "",
        f"分档：{verdict}" if verdict else "",
        f"地点：{loc}" if loc else "",
    ] if p)
    dims_line = f'<div class="dims">{dims}</div>' if dims else ""
    # 四维各自一句依据：让"为什么是这个分"可查
    sr = advice.get("score_reasons") or {}
    dim_keys = [("technical", "技术"), ("experience", "经验"), ("behavioral", "软实力"), ("career", "职业")]
    score_rows = "".join(
        f"<li><b>{k_cn} {esc((advice.get('scores') or {}).get(k, '?'))}：</b>{esc(sr[k])}</li>"
        for k, k_cn in dim_keys
        if sr.get(k)
    )
    score_block = f"<h2>四维依据</h2><ul>{score_rows}</ul>" if score_rows else ""
    loc_block = (
        f"<h2>地点说明</h2><p>{esc(advice.get('location_note', ''))}</p>"
        if advice.get("location_note") else ""
    )
    loc_warn = '<div class="warn">地点判定 FAIL，已一票否决</div>' if advice.get("location_verdict") == "FAIL" else ""
    note_block = f"<h2>备注</h2><p>{esc(advice.get('note', ''))}</p>" if advice.get("note") else ""

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", "SimSun", sans-serif;
         font-size: 14px; line-height: 1.7; margin: 40px; }}
  h1 {{ font-size: 20px; }}
  .meta {{ color: #555; }}
  .dims {{ margin: 6px 0; color: #333; }}
  .warn {{ margin: 6px 0; color: #b91c1c; font-weight: 700; }}
  ul, ol {{ margin: 6px 0; }}
</style></head>
<body>
<h1>投递建议：{esc(advice.get('position', ''))}</h1>
<div class="meta">{meta}</div>
{dims_line}
{score_block}
{loc_block}
{loc_warn}
<h2>理由</h2><ol>{reasons}</ol>
<h2>差什么</h2><ul>{gaps}</ul>
<h2>怎么补</h2><p>{esc(advice.get('advice', ''))}</p>
<h2>打招呼第一句</h2><p>{esc(advice.get('opening_line', ''))}</p>
{note_block}
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
