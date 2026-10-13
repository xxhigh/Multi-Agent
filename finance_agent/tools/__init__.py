"""데이터 수집·계산 도구 (OpenDART 기업개황·공시·감사보고서, 문서 풀 RAG).

에이전트 로직과 분리된 순수 도구 계층이다. 에이전트는 이 모듈의 도구 묶음만 가져다 쓴다.
"""

from .startup_tools import (
    FINANCIAL_TABLE_MARK,
    INVESTMENT_TOOLS,
    MARKET_TOOLS,
    RESEARCHER_TOOLS,
)

# 도구 이름 → 근거(Evidence) 종류.
# 여기에 없는 도구(search_company, calculator)의 결과는 보고서 근거로 저장하지 않는다.
EVIDENCE_TYPE_BY_TOOL = {
    "get_company_overview": "overview",
    "get_recent_disclosures": "disclosure",
    "search_company_docs": "company_doc",
    "search_industry_docs": "industry_doc",
    "get_audit_financials": "financials",
    "search_audit_report": "audit_notes",
}

# 근거 종류 → 출처 (편향 통제: 보고서 전체 출처 다양성 검사에 사용)
PROVIDER_BY_TYPE = {
    "overview": "DART 기업개황",
    "disclosure": "DART 공시",
    "company_doc": "기업 자료(IR·기사)",
    "industry_doc": "산업 리포트",
    "financials": "DART 감사보고서 재무제표",
    "audit_notes": "DART 감사보고서 주석",
}

# 근거 종류 → 이 근거를 모으는 담당 에이전트 (재작업 대상 결정에 사용)
COLLECTOR_BY_TYPE = {
    "overview": "researcher",
    "disclosure": "researcher",
    "company_doc": "researcher",
    "industry_doc": "market_researcher",
    "financials": "investment_analyst",
    "audit_notes": "investment_analyst",
}

__all__ = [
    "COLLECTOR_BY_TYPE",
    "EVIDENCE_TYPE_BY_TOOL",
    "FINANCIAL_TABLE_MARK",
    "INVESTMENT_TOOLS",
    "MARKET_TOOLS",
    "PROVIDER_BY_TYPE",
    "RESEARCHER_TOOLS",
]
