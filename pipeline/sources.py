"""岗位搜集 L1：按「岗位类别 + 城市」机械拼出各招聘平台的搜索链接。

只生成链接，不抓取、不登录、不投递 —— 零风险，也不会因为平台改版而坏。
链接结构可能随平台改版失效，打不开就退回平台首页手动搜关键词。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# BOSS 直聘的城市编码（只放常用的，未知城市就不带 city 参数，让用户在页面上选）
BOSS_CITY = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "南京": "101190100",
    "武汉": "101200100",
    "西安": "101110100",
    "天津": "101030100",
    "苏州": "101190400",
    "长沙": "101250100",
    "重庆": "101040100",
    "合肥": "101220100",
    "厦门": "101230100",
    "青岛": "101120200",
    "郑州": "101180100",
    "无锡": "101190200",
    "东莞": "101281600",
    "佛山": "101280800",
}

PLATFORMS = [
    {
        "name": "BOSS直聘",
        "why": "实习/校招岗最多，聊得快；看完整 JD 通常要登录",
        "url": lambda kw, city: (
            f"https://www.zhipin.com/web/geek/job?query={kw}"
            + (f"&city={BOSS_CITY[city]}" if city in BOSS_CITY else "")
        ),
    },
    {
        "name": "实习僧",
        "why": "实习岗最对口，JD 写得细，适合学生",
        "url": lambda kw, city: f"https://www.shixiseng.com/interns?keyword={kw}" + (
            f"&city={quote(city)}" if city else ""
        ),
    },
    {
        "name": "猎聘",
        "why": "正职岗多、薪资透明；城市在页面上选",
        "url": lambda kw, city: f"https://www.liepin.com/zhaopin/?key={kw}",
    },
    {
        "name": "牛客",
        "why": "校招/实习内推信息集中，能看到面试经验",
        "url": lambda kw, city: "https://www.nowcoder.com/jobs/recommend",
    },
]


def build(categories: list[str], city: str = "") -> list[dict]:
    out = []
    for cat in categories:
        kw = quote(cat)
        for p in PLATFORMS:
            out.append(
                {
                    "category": cat,
                    "platform": p["name"],
                    "why": p["why"],
                    "url": p["url"](kw, city),
                }
            )
    return out


def render_markdown(categories: list[str], city: str, links: list[dict]) -> str:
    head = f"# 岗位搜索链接｜{city or '不限城市'} · {' / '.join(categories)}\n\n"
    head += f"生成时间：{datetime.now():%Y-%m-%d %H:%M}\n\n"
    head += (
        "> 用法：点开链接，挑中岗位后把 JD 正文复制进 `岗位JD\\<公司>-<岗位>.txt`，"
        "再在 `jobs.jsonl` 里登记一行，就能用 `batch --dry-run` 先筛一遍。\n\n"
    )

    body = []
    for cat in categories:
        body.append(f"## {cat}\n")
        for link in links:
            if link["category"] != cat:
                continue
            body.append(f"- **{link['platform']}**（{link['why']}）\n  {link['url']}\n")
        body.append("")

    body.append("## 登记格式（复制到 jobs.jsonl，每行一个岗位）\n")
    body.append("```json")
    body.append('{"title":"AI应用开发实习生","city":"%s","company":"","jd":"岗位职责：……（直接粘文字）"}' % (city or "北京"))
    body.append('{"title":"Agent开发实习生","city":"%s","jd":"file:C:\\\\Users\\\\马\\\\简历助手\\\\岗位JD\\\\某岗位.txt"}' % (city or "北京"))
    body.append("```\n")
    body.append("> `company` 留空 → 自动跳过第 3 步，省一次调用。")
    return head + "\n".join(body)


def write_files(categories: list[str], city: str = "", out: str | Path | None = None) -> Path:
    links = build(categories, city)
    if out is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = Path(__file__).resolve().parent.parent / "data" / "runs" / ts
        base.mkdir(parents=True, exist_ok=True)
        out = base / "search_links.md"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(categories, city, links), encoding="utf-8")
    return out


def collect_dir(folder: str | Path, *, out: str | Path | None = None, city: str = "") -> dict:
    """把「岗位JD」目录里每个 .txt 变成一个岗位，写成 jobs.jsonl。

    文件名即岗位名（如 `AI应用开发实习生（北京）.txt`），城市从括号里取，取不到用 --city。
    """
    import json

    from . import prefilter

    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"目录不存在：{folder}")
    files = sorted(p for p in folder.glob("*.txt") if not p.name.startswith("搜索链接"))
    rows, skipped = [], []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) < 20:
            skipped.append((f.name, f"内容太短（{len(text)} 字），像空文件或只有标题"))
            continue
        title = f.stem
        rows.append(
            {
                "title": title,
                "city": prefilter.city_of({"title": title}) or city,
                "company": "",
                "jd": text,
            }
        )
    if out is None:
        out = folder / "jobs.jsonl"
    out = Path(out)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
    return {"out": str(out), "count": len(rows), "skipped": skipped, "rows": rows}
