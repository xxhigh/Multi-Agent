"""에이전트별 프롬프트 템플릿. 로직 코드와 분리해 문구만 따로 고칠 수 있게 둔다."""

from .report import JUDGE_PROMPT, REVISION_PROMPT, WRITER_PROMPT
from .supervisor import INTAKE_PROMPT, ROUTING_PROMPT
from .workers import ANALYST_PROMPT, RESEARCHER_PROMPT, STRUCTURING_PROMPT

__all__ = [
    "ANALYST_PROMPT",
    "INTAKE_PROMPT",
    "JUDGE_PROMPT",
    "RESEARCHER_PROMPT",
    "REVISION_PROMPT",
    "ROUTING_PROMPT",
    "STRUCTURING_PROMPT",
    "WRITER_PROMPT",
]
