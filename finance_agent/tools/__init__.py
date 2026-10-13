"""데이터 수집·계산 도구 (OpenDART, Yahoo Finance, 사업보고서 RAG).

에이전트 로직과 분리된 순수 도구 계층이다. 에이전트는 이 모듈의 도구 묶음만 가져다 쓴다.
"""

from .finance_tools import (
    ANALYST_TOOLS,
    FINANCIAL_TABLE_MARK,
    RESEARCH_TOOLS,
)

# 도구 이름 → 근거(Evidence) 종류.
# 여기에 없는 도구(search_company, calculator)의 결과는 보고서 근거로 저장하지 않는다.
EVIDENCE_TYPE_BY_TOOL = {
    "search_business_report": "dart_report",
    "get_financial_statements": "statements",
    "get_company_overview": "overview",
    "get_price_summary": "price",
    "get_recent_disclosures": "disclosure",
    "calculate_financial_ratios": "ratios",
}

# 근거 종류 → 출처 기관 (편향 통제: 관점별 출처 다양성 검사에 사용)
PROVIDER_BY_TYPE = {
    "dart_report": "DART 사업보고서",
    "statements": "DART 재무제표",
    "overview": "DART 기업개황",
    "disclosure": "DART 공시",
    "price": "Yahoo Finance",
    "ratios": "DART 재무제표 기반 계산",
}

# 근거 종류 → 이 근거를 모으는 담당 에이전트 (재작업 대상 결정에 사용)
COLLECTOR_BY_TYPE = {
    "dart_report": "researcher",
    "statements": "researcher",
    "overview": "researcher",
    "disclosure": "researcher",
    "price": "researcher",
    "ratios": "analyst",
}

__all__ = [
    "ANALYST_TOOLS",
    "COLLECTOR_BY_TYPE",
    "EVIDENCE_TYPE_BY_TOOL",
    "FINANCIAL_TABLE_MARK",
    "PROVIDER_BY_TYPE",
    "RESEARCH_TOOLS",
]
