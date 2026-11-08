"""JD 文本完整性守卫：清洗不可见字符，检测 prompt 注入迹象。

适配自 ai-job-search（MIT License, © Mads Lorentzen）的 tools/posting_guard.py，
只保留纯文本路径最相关的三类检查：
  1. 不可见字符（零宽、bidi 控制、Unicode tag U+E0000-U+E007F、BOM 等）—— 清除；
  2. 注入指令行（「忽略之前的指令」「你是 AI 助手」「运行以下命令」等）—— 标记；
  3. 混合同形异体词（拉丁字母混西里尔/希腊字母的伪装词）—— 标记。

HTML 隐藏内容（display:none 等）不由本模块处理：collect 用 Playwright 的
inner_text 只取可见文本，天然不含 CSS 隐藏内容。
"""
from __future__ import annotations

import re

# —— 不可见字符集合 ——
ZERO_WIDTH = "​‌‍⁠⁡⁢⁣⁤"
BIDI = "‎‏‪‫‬‭‮⁦⁧⁨⁩"
BIDI_TAMPER = "‭‮⁦⁧⁨⁩"  # override + isolate
FORMAT = "­  "
BOM = "﻿"
TAG_CHARS = "".join(chr(c) for c in range(0xE0000, 0xE0080))
INVISIBLE = ZERO_WIDTH + BIDI + FORMAT + BOM + TAG_CHARS
TAG_RUN = re.compile("[\U000E0000-\U000E007F]+")

# —— 注入指令模式（摘自 posting_guard.py）——
INSTRUCTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"\bignore (all|any|the|previous|prior|above|earlier)\b",
    r"\b(disregard|forget)( all| any| the| your| my)? (previous|prior|above|earlier|preceding|initial|original|system) (instructions?|prompts?)\b",
    r"\byou are (?:an?|the) (?:ai(?![\w-])|ai (?:assistant|agent|model|language model)|assistant|llm|language model)\b",
    r"\bsystem prompt\b",
    r"\b(do not|don['’]?t|never) (tell|inform) the user\b(?!['’]s)",
    r"\b(do not|don['’]?t|never) (mention|reveal|disclose) (this|these|that|it|any of this|the (above|following|previous))\b[^.\n]{0,30}\bto the user\b",
    r"\b(run|execute) the following (command|commands|script|code|shell|instruction|instructions|steps)\b",
    r"\bas an ai(?![\w-])(?![\s/&]+(?:[\w&/-]+\s+){0,3}?(?:engineer|developer|researcher|scientist|architect|specialist|lead|consultant|manager|analyst|intern|trainer|expert|strategist|designer|tester|owner|programmer)s?\b)",
    r"^\s*(assistant|system|user)\s*:",
    r"<\|",
    r"\[INST\]",
    r"###\s*(system|instruction)",
    r"\b(curl|wget|ssh|scp)\b",
    r"\.env\b",
    r"settings\.json",
    r"allowed-tools",
    r"\btool_use\b|\bfunction_call\b",
    # 中文注入（国内 JD 场景补充）
    r"忽略\s*(所有|任何|之前|前面|以上|先前|上述)的?\s*(指令|提示|要求)",
    r"你(?:现在)?(?:是|成为)(?:一个|名)?(?:AI|人工智能|大模型)?(?:助手|助理)",
    r"系统提示",
    r"不要\s*(告诉|告知|透露|提及)\s*用户",
    r"(?:运行|执行)\s*以下\s*(?:命令|指令|脚本|代码)",
)]

WORD = re.compile(r"\w+", re.UNICODE)
LATIN = re.compile(r"[A-Za-z]")
CONFUSABLE = re.compile(r"[Ѐ-ӿͰ-Ͽ]")  # Cyrillic + Greek

EXCERPT = 120
LINE_MAX = 200


def _fold(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\xa0\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _excerpt(line: str, at: int) -> str:
    if len(line) <= LINE_MAX:
        return line
    lead = LINE_MAX // 3
    start = max(0, at - lead)
    end = min(len(line), start + LINE_MAX)
    start = max(0, end - LINE_MAX)
    return ("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")


def guard(raw: str) -> tuple[str, dict]:
    """清洗并扫描文本。返回 (清洗后文本, findings)，findings 含 verdict。"""
    text = _fold(raw)

    counts: dict = {}
    flagged = 0
    for key, chars in (("zero_width", ZERO_WIDTH), ("bidi", BIDI), ("format", FORMAT), ("bom", BOM)):
        n = sum(text.count(c) for c in chars)
        if n:
            counts[key] = n
    tags = sum(1 for ch in text if "\U000E0000" <= ch <= "\U000E007F")
    if tags:
        counts["tag"] = tags

    for i, ch in enumerate(text):
        if ch in BIDI_TAMPER:
            flagged += 1
        elif ch in ZERO_WIDTH or ch == BOM:
            before = text[i - 1] if i else ""
            after = text[i + 1] if i + 1 < len(text) else ""
            if before.isalpha() and after.isalpha():
                flagged += 1

    invisible_text = []
    for run in TAG_RUN.findall(text):
        decoded = "".join(
            chr(ord(c) - 0xE0000) for c in run if 0x20 <= ord(c) - 0xE0000 < 0x7F
        ).strip()
        if decoded:
            invisible_text.append(decoded[:LINE_MAX])

    text = text.translate({ord(c): None for c in INVISIBLE})

    instruction_like = []
    seen = set()
    for line in text.splitlines() + invisible_text:
        s = line.strip()
        if not s or s in seen:
            continue
        m = next((m for m in (p.search(s) for p in INSTRUCTION_PATTERNS) if m), None)
        if m:
            seen.add(s)
            instruction_like.append(_excerpt(s, m.start()))

    homoglyphs = []
    for w in WORD.findall(text + "\n" + "\n".join(invisible_text)):
        if LATIN.search(w) and CONFUSABLE.search(w) and w not in homoglyphs:
            homoglyphs.append(w)

    findings = {
        "invisible_chars": counts,
        "invisible_flagged": flagged,
        "invisible_text": invisible_text,
        "instruction_like": instruction_like,
        "homoglyph_runs": homoglyphs,
    }
    findings["verdict"] = _verdict(findings)
    return text, findings


def _verdict(f: dict) -> str:
    alarms = []
    if f.get("instruction_like"):
        alarms.append(f"{len(f['instruction_like'])} 行疑似注入指令")
    if f.get("invisible_flagged"):
        alarms.append(f"{f['invisible_flagged']} 处词内不可见字符")
    if (f.get("invisible_chars") or {}).get("tag"):
        alarms.append(f"{f['invisible_chars']['tag']} 个 Unicode tag 字符")
    if f.get("invisible_text"):
        alarms.append(f"{len(f['invisible_text'])} 段不可见字符文本")
    if f.get("homoglyph_runs"):
        alarms.append(f"{len(f['homoglyph_runs'])} 个混合同形异体词")
    if alarms:
        return "guard: ⚠ " + "、".join(alarms) + " —— 建议人工复核"
    return "guard: clean"
