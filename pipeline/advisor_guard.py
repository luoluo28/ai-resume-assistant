"""简历顾问输出的机械校验（防编造），不依赖模型自觉。

校验项（与 prompts/06_resume_advisor.md 的红线一一对应）：
1. rewrites 必须有可在简历原文里逐字找到的 source_quote；
2. rewrites 的 after 不得出现原文/画像里没有的英文专有名词（工具/产品名）；
3. rewrites 的 after 不得出现原文里没有的数字；
4. verdict=none 的 JD 要求不得出现在 rewrites；
5. 个人项目不得出现「带领团队/主导」等角色词；
6. hooks 的 must_be_true_fact 必须能在原文里找到（连续 6 字子串）；
7. interview_probe 中，简历里完全没有该关键词时，can_answer 不得为 yes。

被丢弃的条目记入 result["_guard"]，便于复查；不抛异常，保证主流程可用。
"""
from __future__ import annotations

import re

_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,}")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_ROLE_WORDS = ("带领团队", "主导跨部门", "负责产品线", "带领", "统筹团队")
_REQ_PREFIX = ("熟悉", "了解", "掌握", "精通", "有", "具备", "能", "会", "可以", "要求")
_REQ_SUFFIX = ("经验", "能力", "优先", "相关", "以上", "及以上", "者")


def _core_of_item(item: str) -> str:
    """把 JD 要求句剥成核心词（如「有用户增长经验」→「用户增长」）。"""
    s = _norm(item)
    for p in _REQ_PREFIX:
        if s.startswith(p):
            s = s[len(p):]
    for suf in _REQ_SUFFIX:
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def _latin_terms(text: str) -> set:
    return {m.group(0).lower() for m in _LATIN_RE.finditer(text or "")}


def _nums(text: str) -> set:
    return {m.group(0) for m in _NUM_RE.finditer(text or "")}


def _has_substring(haystack: str, needle: str, n: int = 6) -> bool:
    """needle 的任意连续 n 字片段是否出现在 haystack（中文场景的宽松核实）。"""
    h = _norm(haystack)
    s = _norm(needle)
    if not s:
        return False
    if len(s) <= n:
        return s in h
    return any(s[i : i + n] in h for i in range(0, len(s) - n + 1))


_LIST_KEYS = (
    "alignments",
    "rewrites",
    "evidence_plan",
    "interview_probe",
    "project_rank",
    "project_blocks",
    "hooks",
    "missing_material",
)


def sanitize(data: dict, *, resume_text: str, profile: dict, position: str = "") -> dict:
    dropped: list = []
    resume_norm = _norm(resume_text)
    skills = str((profile or {}).get("skills", ""))
    allowed_latin = _latin_terms(skills) | _latin_terms(resume_text) | _latin_terms(str(profile))
    allowed_nums = _nums(resume_text) | _nums(str(profile))

    # 结构收敛：所有块统一为 list[dict]，避免模型返回 str/dict 时下游崩
    for k in _LIST_KEYS:
        v = data.get(k)
        if isinstance(v, dict):
            v = [v]
        if not isinstance(v, list):
            v = []
        data[k] = [x if isinstance(x, dict) else {"item": str(x)} for x in v]

    none_items: list = []
    for a in data.get("alignments") or []:
        if isinstance(a, dict) and str(a.get("verdict", "")) == "none":
            item = str(a.get("jd_item", ""))
            none_items.append((_norm(item), _core_of_item(item)))

    # ---- rewrites ----
    rewrites = []
    raw_rewrites = data.get("rewrites")
    for rw in raw_rewrites if isinstance(raw_rewrites, list) else []:
        if not isinstance(rw, dict):
            continue
        src = str(rw.get("source_quote", "") or "")
        after = str(rw.get("after", "") or "")
        before = str(rw.get("before", "") or "")
        if not src or _norm(src) not in resume_norm:
            dropped.append({"part": "rewrites", "reason": "source_quote 在原文中找不到", "before": before[:60]})
            continue
        if before and _norm(before) not in resume_norm:
            dropped.append({"part": "rewrites", "reason": "before 不是原文逐字片段", "before": before[:60]})
            continue
        bad_latin = [t for t in _latin_terms(after) if t not in allowed_latin]
        if bad_latin:
            dropped.append({"part": "rewrites", "reason": "出现原文没有的专有名词: " + ",".join(bad_latin[:3]), "before": before[:60]})
            continue
        bad_nums = [n for n in _nums(after) if n not in allowed_nums]
        if bad_nums:
            dropped.append({"part": "rewrites", "reason": "出现原文没有的数字: " + ",".join(bad_nums[:3]), "before": before[:60]})
            continue
        if any(w in after for w in _ROLE_WORDS) and not any(w in resume_text for w in ("带领", "主导")):
            dropped.append({"part": "rewrites", "reason": "出现与事实不符的角色词", "before": before[:60]})
            continue
        hit_none = False
        after_norm = _norm(after)
        for full, core in none_items:
            if full and full in after_norm:
                hit_none = True
                break
            if core and len(core) >= 2 and core in after_norm and core not in resume_norm:
                hit_none = True
                break
        if hit_none:
            dropped.append({"part": "rewrites", "reason": "把判定为 none 的要求写进了改写", "before": before[:60]})
            continue
        rewrites.append(rw)
    data["rewrites"] = rewrites

    # ---- hooks ----
    hooks = []
    raw_hooks = data.get("hooks")
    for h in raw_hooks if isinstance(raw_hooks, list) else []:
        if not isinstance(h, dict):
            continue
        fact = str(h.get("must_be_true_fact", "") or "")
        if not _has_substring(resume_text, fact):
            dropped.append({"part": "hooks", "reason": "must_be_true_fact 在原文中找不到", "resume_line": str(h.get("resume_line", ""))[:60]})
            continue
        hooks.append(h)
    data["hooks"] = hooks

    # ---- interview_probe：否定乐观自评 ----
    probes = []
    raw_probes = data.get("interview_probe")
    for p in raw_probes if isinstance(raw_probes, list) else []:
        if not isinstance(p, dict):
            continue
        kw = str(p.get("keyword", "") or "")
        if str(p.get("can_answer", "")) == "yes" and kw:
            kw_latin = _latin_terms(kw)
            if kw_latin:
                present = any(t in allowed_latin for t in kw_latin)
            else:
                present = _norm(kw)[:4] in resume_norm if len(_norm(kw)) >= 2 else False
            if not present:
                p["can_answer"] = "no"
                p["advice"] = (str(p.get("advice", "")) + "（简历中无该关键词证据，按不会处理）").strip()
        probes.append(p)
    data["interview_probe"] = probes

    data["_guard"] = {
        "dropped_count": len(dropped),
        "dropped": dropped[:12],
        "position": position,
    }
    return data


def render_markdown(adv: dict) -> str:
    """把顾问结果渲染成可复制的 Markdown（便于贴进简历/带去面试）。"""
    out: list = []
    out.append("# 简历顾问结果\n")
    for a in adv.get("alignments", []):
        out.append(f"- [{a.get('verdict')}] {a.get('jd_item')} → {a.get('action')}")
        if a.get("evidence"):
            out.append(f"    - 证据：{a['evidence']}")

    if adv.get("project_blocks"):
        out.append("\n## 项目块（可直接替换简历）\n")
        for b in adv["project_blocks"]:
            if not isinstance(b, dict):
                continue
            out.append(f"### {b.get('name') or b.get('project') or ''}")
            for key, label in (
                ("one_liner", "做什么"),
                ("role", "我的角色"),
                ("decision", "关键决策"),
                ("implementation", "关键实现"),
                ("deliverable", "交付物"),
            ):
                if b.get(key):
                    out.append(f"- **{label}**：{b[key]}")

    if adv.get("rewrites"):
        out.append("\n## 改写对照\n")
        for rw in adv["rewrites"]:
            out.append(f"- 原句：{rw.get('before', '')}")
            out.append(f"  - 改后：{rw.get('after', '')}")
            out.append(f"  - 面试可能追问：{rw.get('risk', '')}")

    if adv.get("evidence_plan"):
        out.append("\n## 一周内能补的证据\n")
        for e in adv["evidence_plan"]:
            out.append(f"- 缺：{e.get('gap', '')}")
            out.append(f"  - 交付：{e.get('deliverable', '')}（验收：{e.get('acceptance', '')}）")
            out.append(f"  - 补完可写：{e.get('resume_line', '')}")

    if adv.get("interview_probe"):
        out.append("\n## 面试拷问自检\n")
        for p in adv["interview_probe"]:
            out.append(f"- {p.get('keyword', '')}｜{p.get('question', '')} → 能答：{p.get('can_answer', '')}")
            if p.get("advice"):
                out.append(f"  - {p['advice']}")

    tree = adv.get("interview_tree")
    if tree:
        out.append("\n## 追问树\n")
        items = tree if isinstance(tree, list) else [tree]
        for t in items:
            if not isinstance(t, dict):
                out.append(f"- {t}")
                continue
            out.append(f"### {t.get('project', '')}")
            for layer in t.get("layers", []) or []:
                if not isinstance(layer, dict):
                    out.append(f"- {layer}")
                    continue
                lv = int(layer.get("level", 1) or 1)
                out.append(f"{'  ' * (lv - 1)}- L{lv}：{layer.get('question')}（{layer.get('readiness')}）")
                for pt in layer.get("answer_points", []) or []:
                    out.append(f"{'  ' * lv}  · {pt}")

    if adv.get("hooks"):
        out.append("\n## 可埋的钩子\n")
        for h in adv["hooks"]:
            out.append(f"- 简历里写：{h.get('resume_line', '')}")
            out.append(f"  - 会引出：{h.get('expected_question', '')}（依据事实：{h.get('must_be_true_fact', '')}）")

    g = adv.get("_guard") or {}
    if g.get("dropped_count"):
        out.append(f"\n> 合规闸门丢弃 {g['dropped_count']} 条（无原文出处/数字或名词不符）")
    return "\n".join(out)
