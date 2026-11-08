"""流水线数据结构。json_schema_version: resume-advice-v1。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

JSON_SCHEMA_VERSION = "resume-advice-v1"


def _str(v):
    return str(v).strip() if v is not None else ""


def _list(v):
    if isinstance(v, list):
        return v
    return [] if v is None else [v]


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "是")
    return bool(v)


def _dict(v):
    return v if isinstance(v, dict) else {}


@dataclass
class Profile:
    """第一步 profile_extract 的输出：简历画像。"""

    target_position: str = ""
    education: str = ""
    skills: str = ""
    projects: str = ""
    experience: str = ""
    missing: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            target_position=_str(d.get("target_position")),
            education=_str(d.get("education")),
            skills=_str(d.get("skills")),
            projects=_str(d.get("projects")),
            experience=_str(d.get("experience")),
            missing=[_str(x) for x in _list(d.get("missing"))],
        )

    def to_dict(self):
        return asdict(self)


@dataclass
class JdAnalysis:
    """第二步 jd_analysis 的输出：岗位需求拆解。"""

    hard_requirements: list = field(default_factory=list)
    keywords_weighted: list = field(default_factory=list)
    soft_skills: list = field(default_factory=list)
    veto: dict = field(default_factory=lambda: {"blocked": False, "reason": ""})
    jd_summary: str = ""

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            hard_requirements=_list(d.get("hard_requirements")),
            keywords_weighted=_list(d.get("keywords_weighted")),
            soft_skills=[_str(x) for x in _list(d.get("soft_skills"))],
            veto=_dict(d.get("veto")),
            jd_summary=_str(d.get("jd_summary")),
        )

    def to_dict(self):
        return asdict(self)


@dataclass
class CompanyValues:
    """第三步 company_values 的输出：企业价值观分析。"""

    need_company_info: bool = False
    labels: list = field(default_factory=list)
    hire_profile: str = ""

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            need_company_info=_bool(d.get("need_company_info")),
            labels=_list(d.get("labels")),
            hire_profile=_str(d.get("hire_profile")),
        )

    def to_dict(self):
        return asdict(self)


@dataclass
class ApplyAdvice:
    """第四步 apply_advice 的输出：投递建议。score/verdict 由代码按五维加权算出。"""

    position: str = ""
    conclusion: str = ""
    score: int = 0
    verdict: str = ""
    scores: dict = field(default_factory=dict)  # technical/experience/behavioral/career
    location_verdict: str = ""  # PASS/FLAG/FAIL
    reasons: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    advice: str = ""
    opening_line: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            position=_str(d.get("position")),
            conclusion=_str(d.get("conclusion")),
            scores=_dict(d.get("scores")),
            location_verdict=_str(d.get("location_verdict")),
            reasons=[_str(x) for x in _list(d.get("reasons"))],
            gaps=[_str(x) for x in _list(d.get("gaps"))],
            advice=_str(d.get("advice")),
            opening_line=_str(d.get("opening_line")),
            note=_str(d.get("note")),
        )

    def to_dict(self):
        return asdict(self)
