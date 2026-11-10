"""岗位搜集：两层。

L1 `build()` —— 机械拼搜索入口链接。零风险、零成本，但**主流平台对裸链接一律重定向或要登录**
（实测 BOSS 搜索页卡在"加载中"、猎聘丢掉关键词跳回城市首页、实习僧被拦），所以只当"入口"用，
并明确标注哪些需要登录。

L2 `collect_jobs()` —— 对**牛客**做一次真实抓取。牛客搜索结果是服务端渲染的纯 HTML，
用 requests 就能拿到「岗位名 / 薪资 / 城市 / 岗位详情链接」。
**这一步不调用任何模型，不产生 token 费用**；只有把 JD 拿去做诊断才烧 token。

不登录、不投递、不绕风控。抓不到就老实退回 L1。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

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

# 用于从抓取文本里识别城市的名单（与 BOSS_CITY 同源，另补几个常见城市）
CITY_NAMES = list(BOSS_CITY.keys()) + ["苏州", "宁波", "济南", "大连", "福州", "珠海", "香港", "远程"]

PLATFORMS = [
    {
        "name": "牛客",
        "why": "校招/实习岗集中，搜索结果能直接看岗位；可在下方「抓到 N 个岗位」里直接点",
        "needs_login": False,
        "url": lambda kw, city: f"https://www.nowcoder.com/search?query={kw}&type=all",
    },
    {
        "name": "BOSS直聘",
        "why": "实习/校招岗最多，聊得快。网页版搜索页要先登录才会加载岗位，手机上用 App 搜更顺",
        "needs_login": True,
        "url": lambda kw, city: (
            f"https://www.zhipin.com/web/geek/job?query={kw}"
            + (f"&city={BOSS_CITY[city]}" if city in BOSS_CITY else "")
        ),
    },
    {
        "name": "实习僧",
        "why": "实习岗最对口，JD 写得细。搜索页需要登录，先在首页登录再搜",
        "needs_login": True,
        "url": lambda kw, city: f"https://www.shixiseng.com/interns?keyword={kw}",
    },
    {
        "name": "猎聘",
        "why": "正职岗多、薪资透明。城市要在页面上手动选（链接带不了）",
        "needs_login": True,
        "url": lambda kw, city: "https://www.liepin.com/zhaopin/",
    },
    {
        "name": "必应搜索",
        "why": "不受平台登录限制，常能直接搜到各平台的岗位详情页，适合「关键词 + 城市」一起搜",
        "needs_login": False,
        "url": lambda kw, city: (
            "https://cn.bing.com/search?q=" + kw + quote(" " + city) + quote(" 实习 招聘")
        ),
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
                    "needs_login": p["needs_login"],
                    "url": p["url"](kw, city),
                }
            )
    return out


# ---------------- L2：牛客真实岗位抓取（0 token） ----------------

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_JOB_ID_RE = re.compile(r'<a[^>]*href="([^"]*?/jobs/detail/(\d+)[^"]*)"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SALARY_RE = re.compile(
    r"(\d+(?:\.\d+)?[-~]\d+(?:\.\d+)?(?:元/天|元/月|元|k/月|K/月|k|K|/天|万/年|万|千/月)|"
    r"\d+(?:\.\d+)?(?:元/天|元/月|k/月|K/月|万/年))"
)
_TYPE_WORDS = {"实习", "校招", "社招", "兼职", "全职", "日常实习", "暑期实习"}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", s)).strip()


def _parse_entry(raw: str) -> dict:
    """把「实习 AI应用开发 300-420元/天 有转正 厦门 5天/周 最少3个月 本科」拆成字段。"""
    salary = ""
    m = _SALARY_RE.search(raw)
    if m:
        salary = m.group(1)
    city = ""
    for c in CITY_NAMES:
        # 城市名两侧不能紧贴字母，避免 "AI" 里误命中
        if re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(c), raw):
            city = c
            break
    parts = raw.split(" ")
    parts = [p for p in parts if p]
    head = []
    for p in parts:
        if p == salary or p == city or p in _TYPE_WORDS:
            if p in _TYPE_WORDS and not head:
                head.append(p)  # 保留「实习/校招」这类前缀
                continue
            if p in _TYPE_WORDS:
                continue
            if p == salary or p == city:
                continue
        if len(head) >= 2:
            break
        head.append(p)
    title = " ".join(head).strip() or parts[0]
    rest = [p for p in parts if p not in (title.split(" "))]
    extra = " ".join([p for p in rest if p not in ("有转正",) and p != salary and p != city][:3]).strip()
    return {"title": title, "salary": salary, "city": city, "extra": extra, "raw": raw}


def niuke_search(keyword: str, timeout: int = 15, retries: int = 3) -> tuple[list[dict], str]:
    """抓一次牛客综合搜索页，抽出岗位。返回 (岗位列表, 错误说明)。不调模型。

    实测：牛客会**交替**返回两种页面 —— 约 5KB 的空壳页（无岗位）和 238KB 的完整页。
    所以按「页面长度」判定是否拿到壳，拿到就重试；最多 retries 次。
    """
    r = None
    last_err = ""
    for attempt in range(retries):
        try:
            r = requests.get(
                f"https://www.nowcoder.com/search?query={quote(keyword)}&type=all",
                headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                timeout=timeout,
            )
        except Exception as e:  # 网络层失败
            last_err = f"请求牛客失败：{type(e).__name__}"
            if attempt < retries - 1:
                time.sleep(0.6)
            continue
        if r.status_code != 200:
            last_err = f"牛客返回 {r.status_code}"
            if attempt < retries - 1:
                time.sleep(0.6)
            continue
        # 空壳页（约 5KB）没有岗位数据，等下一轮
        if len(r.text) < 20000:
            last_err = "拿到空壳页（牛客交替返回），已重试"
            if attempt < retries - 1:
                time.sleep(0.6)
            continue
        break

    if r is None or r.status_code != 200:
        return [], last_err or "未知错误"
    if len(r.text) < 20000:
        return [], "牛客连续返回空壳页，稍后再试一次"

    jobs, seen = [], set()
    for m in _JOB_ID_RE.finditer(r.text):
        jid = m.group(2)
        if jid in seen:
            continue
        raw = _clean(m.group(3))
        if len(raw) < 4:
            continue
        seen.add(jid)
        d = _parse_entry(raw)
        d["url"] = f"https://www.nowcoder.com/jobs/detail/{jid}"
        d["source"] = "牛客"
        d["keyword"] = keyword
        jobs.append(d)
    return jobs, ""


def collect_jobs(categories: list[str], city: str = "", *, per_kw_limit: int = 20) -> dict:
    """对多个岗位大类逐个抓牛客，合并去重；给了城市就按城市过滤。0 token。"""
    all_jobs, errors = [], []
    for cat in categories:
        jobs, err = niuke_search(cat)
        if err:
            errors.append(f"{cat}：{err}")
            continue
        if city:
            # 只筛掉「能识别出城市且明显不是目标城市」的，识别不出的一律保留
            jobs = [j for j in jobs if (not j["city"]) or j["city"] == city]
        all_jobs.extend(jobs[:per_kw_limit])
    dedup, seen = [], set()
    for j in all_jobs:
        if j["url"] in seen:
            continue
        seen.add(j["url"])
        dedup.append(j)
    return {
        "jobs": dedup,
        "count": len(dedup),
        "errors": errors,
        "token_cost": 0,
    }


def render_markdown(categories: list[str], city: str, links: list[dict], jobs: list[dict] | None = None) -> str:
    head = f"# 岗位搜索入口｜{city or '不限城市'} · {' / '.join(categories)}\n\n"
    head += f"生成时间：{datetime.now():%Y-%m-%d %H:%M}\n\n"
    if jobs:
        head += f"## 已抓到的具体岗位（{len(jobs)} 个，零模型调用）\n\n"
        for j in jobs:
            line = f"- **{j['title']}**"
            if j["salary"]:
                line += f"｜{j['salary']}"
            if j["city"]:
                line += f"｜{j['city']}"
            if j.get("extra"):
                line += f"｜{j['extra']}"
            head += line + f"\n  {j['url']}\n"
        head += "\n> 点开岗位页 → 复制 JD 正文 → 粘进「简历诊断」，就能得到分数和差距。\n\n"
    head += "## 各平台搜索入口\n\n"
    body = []
    for cat in categories:
        body.append(f"### {cat}\n")
        for link in links:
            if link["category"] != cat:
                continue
            tag = "（需登录）" if link.get("needs_login") else ""
            body.append(f"- **{link['platform']}**{tag}（{link['why']}）\n  {link['url']}\n")
        body.append("")
    body.append("> 平台链接结构会随改版失效；打不开就退回平台首页手动搜关键词。\n")
    return head + "\n".join(body)


def write_files(categories: list[str], city: str = "", out: str | Path | None = None,
                jobs: list[dict] | None = None) -> Path:
    links = build(categories, city)
    if out is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = Path(__file__).resolve().parent.parent / "data" / "runs" / ts
        base.mkdir(parents=True, exist_ok=True)
        out = base / "search_links.md"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(categories, city, links, jobs), encoding="utf-8")
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
