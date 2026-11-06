"""流水线四步。切片 3 先实现第一步 profile_extract，其余步骤随切片 4 串联。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import llm, schema

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    """读取 prompt 文件，去掉文档头（第一个 --- 之前），只留 prompt 正文。"""
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    if "---" in text:
        text = text.split("---", 1)[1]
    return text.strip()


def profile_extract(resume_text: str) -> dict:
    """第一步：从简历文本提取结构化画像。返回 profile dict，或带 error 的兜底 dict。"""
    system = _load_prompt("01_profile_extract.md").replace("{{RESUME_TEXT}}", resume_text)
    data = llm.chat_json(system=system, user="", temperature=0.1)
    if "error" in data:
        return data
    return schema.Profile.from_dict(data).to_dict()


if __name__ == "__main__":
    from . import ingest

    if len(sys.argv) != 2:
        print("用法: python -m pipeline.steps <简历文件路径>")
        sys.exit(2)
    text = ingest.ingest(sys.argv[1])
    print(json.dumps(profile_extract(text), ensure_ascii=False, indent=2))
