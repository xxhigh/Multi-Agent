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

# ── 보고서 관점 (과제 노션 4관점 ↔ 재무 분석 매핑) ───────────
# key: (보고서 제목, 노션 관점명, 작성 담당, 충분성 판정에 필요한 근거 종류)
PERSPECTIVES: dict[str, dict] = {
    "business_tech": {
        "title": "사업·기술 경쟁력",
        "notion_view": "기술 성숙도",
        "owner": "researcher",
        "required": ("dart_report",),
        "guide": "사업 구조, 주요 제품·서비스, 부문별 매출 비중, 연구개발 현황",
    },
    "market": {
        "title": "성장성·시장 평가",
        "notion_view": "시장성",
        "owner": "analyst",
        "required": ("price", "ratios"),
        "guide": "매출·이익 성장률, 주가 흐름, 시가총액·PER·PBR 등 시장 평가 지표",
    },
    "stakeholder": {
        "title": "주주·지배구조·공시",
        "notion_view": "이해관계자",
        "owner": "researcher",
        "required": ("disclosure", "overview"),
        "guide": "최근 주요 공시(실적·투자·자금조달·지배구조), 배당, 대표자·시장 구분 등 이해관계자 관련 정보",
    },
    "financial_health": {
        "title": "재무 건전성·산업 특성",
        "notion_view": "도메인 적용",
        "owner": "analyst",
        "required": ("ratios", "statements"),
        "guide": "수익성·안정성·현금흐름 지표와 업종 특성(예: 금융사는 ROE·PBR 중심)을 반영한 해석",
    },
}
PERSPECTIVE_KEYS = tuple(PERSPECTIVES)

WORKERS = ("researcher", "analyst")
