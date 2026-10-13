"""실행 상한·관점 정의 등 코드로 강제하는 정책 값.

종료 조건과 재작업 한도는 프롬프트가 아니라 여기 상수로 박아 둔다 (LLM이 무시할 수 없게).
"""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path(os.getenv("FINANCE_AGENT_OUTPUT_DIR", ROOT_DIR / "outputs"))
REPORT_DIR = OUTPUT_DIR / "reports"
RUN_DIR = OUTPUT_DIR / "runs"

# ── 종료 보장 ──────────────────────────────────────────────
MAX_STEPS = 16          # supervisor 판단 횟수 상한 (넘으면 강제 마무리)
MAX_ATTEMPTS = 3        # 관점별 최대 작업 횟수 = 최초 1회 + 재작업 2회
MAX_REVISIONS = 2       # 품질 평가 미달 시 보고서 수정·재조사 최대 횟수
MAX_TOOL_ROUNDS = 8     # 하위 에이전트(ReAct) 1회 실행 안 도구 호출 상한
RECURSION_LIMIT = 80    # LangGraph 자체 안전망 (위 상한이 먼저 걸리도록 넉넉히)

DOCS_DIR = Path(os.getenv("FINANCE_AGENT_DOCS_DIR", ROOT_DIR / "data" / "docs"))
MAX_PDF_PAGES = 10      # 제출 조건: 평가 보고서 최대 10장

# ── 보고서 관점 (지난 RAG 과제 목차와 동일) ─────────────────────
# key: (보고서 절 제목, 작성 담당, 충분성 판정에 필요한 근거 종류, 작성 가이드)
PERSPECTIVES: dict[str, dict] = {
    "company_team": {
        "title": "COMPANY & TEAM",
        "owner": "researcher",
        "required": ("overview", "company_doc"),
        "guide": "설립·소재지·대표자(창업자), 조직·인증, 투자 유치 이력(공시의 유상증자 포함), 상장 준비 상태",
    },
    "technology": {
        "title": "TECHNOLOGY",
        "owner": "researcher",
        "required": ("company_doc",),
        "guide": "제품 라인업, 핵심 기술(센서·자율주행·안전 인증), 사양 수치, 연구개발 체계, 기술의 검증 수준(자사 주장 vs 외부 검증)",
    },
    "market": {
        "title": "MARKET",
        "owner": "market_researcher",
        "required": ("industry_doc",),
        "guide": "대상 시장(물류·제조 로봇 등)의 규모·성장률, 수요 동인, 비즈니스 모델(RaaS 등), 경쟁 구도, 대상 기업의 시장 내 위치",
    },
    "investment": {
        "title": "INVESTMENT ASSESSMENT & RISKS",
        "owner": "investment_analyst",
        "required": ("financials", "audit_notes"),
        "guide": "매출·손익 추세, 자본잠식·현금 소진 기간, 투자조건(상환전환우선주 등), 계속기업 불확실성, 핵심 리스크",
    },
}
PERSPECTIVE_KEYS = tuple(PERSPECTIVES)

WORKERS = ("researcher", "market_researcher", "investment_analyst")

# ── 투자 평가 스코어카드 (지난 RAG 과제 가중치, 합계 100) ─────────
SCORECARD: dict[str, dict] = {
    "founder": {"label": "창업자", "weight": 5},
    "market": {"label": "시장성", "weight": 30},
    "product_tech": {"label": "제품·기술력", "weight": 25},
    "competitive_edge": {"label": "경쟁 우위", "weight": 20},
    "traction": {"label": "실적", "weight": 10},
    "deal_terms": {"label": "투자조건", "weight": 10},
}
