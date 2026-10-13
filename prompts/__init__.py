"""에이전트별 시스템 프롬프트 템플릿.

본문은 같은 폴더의 <agent>.md 파일에 있다. `{today}` 와 `{criteria}` 는 호출 시점에 format 으로 채운다.
"""

from pathlib import Path

_DIR = Path(__file__).resolve().parent


def _load(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


SUPERVISOR_PROMPT = _load("supervisor")
COMPANY_RESEARCHER_PROMPT = _load("company_researcher")
TECH_RESEARCHER_PROMPT = _load("tech_researcher")
MARKET_RESEARCHER_PROMPT = _load("market_researcher")
EVALUATOR_PROMPT = _load("evaluator")
WRITER_PROMPT = _load("writer")
