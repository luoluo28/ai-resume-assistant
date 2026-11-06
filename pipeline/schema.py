"""流水线数据结构。json_schema_version: resume-advice-v1。

切片 3 只用 Profile；其余步骤的数据结构随切片 4 补齐。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

JSON_SCHEMA_VERSION = "resume-advice-v1"


@dataclass
class Profile:
    """第一步 profile_extract 的输出：简历画像。"""

    target_position: str = ""
    education: str = ""
    skills: str = ""
    projects: str = ""
    experience: str = ""
    missing: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Profile":
        d = d or {}

        def s(v) -> str:
            return str(v).strip() if v is not None else ""

        missing = d.get("missing", [])
        if not isinstance(missing, list):
            missing = []
        return cls(
            target_position=s(d.get("target_position")),
            education=s(d.get("education")),
            skills=s(d.get("skills")),
            projects=s(d.get("projects")),
            experience=s(d.get("experience")),
            missing=[str(x) for x in missing],
        )

    def to_dict(self) -> dict:
        return asdict(self)
