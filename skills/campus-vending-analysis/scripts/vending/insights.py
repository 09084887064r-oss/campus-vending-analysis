"""结论结构：前 3 大发现 / 行动建议 / 风险预警（只放数据结构与渲染用的文本）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Finding:
    rank: int
    title: str
    detail: str
    evidence: str = ""


@dataclass
class Action:
    priority: str
    action: str
    basis: str = ""
    expected: str = ""          # 预期效果（可量化）
    verify: str = ""            # 验证方式
    owner: str = ""             # 责任对象


@dataclass
class Risk:
    level: str
    risk: str
    impact: str
    mitigation: str


@dataclass
class Narrative:
    summary: str
    findings: list[Finding] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)
