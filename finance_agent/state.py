"""그래프 State 스키마.

설계 원칙
- State에는 "제어 흐름 + 재개에 필요한 것"만 담는다. 도구 원문·결정 로그 전문은 넣지 않는다.
- 작업 페이로드(무엇을 알아냈나)와 제어 메타(다음에 무엇을 할까)를 필드 단위로 분리한다.
- 결정 로그는 observability 계층(JSONL + LangSmith)으로 내보내고 trace_id로 State와 잇는다.

| 판정 포인트      | 반영 위치                                                               |
|-----------------|------------------------------------------------------------------------|
| 제어 vs 페이로드 | 아래 두 블록으로 분리. supervisor는 coverage·sufficiency·카운터만 보고 라우팅 |
| 관측성 위치      | State엔 최신 결정 1건(decision)만, 전체 로그는 observability.log_decision  |
| 지속성 비용      | findings는 에이전트별 최신 결과로 덮어씀, sources는 메타데이터만, 도구 원문 미저장 |
| 상관            | trace_id → LangSmith metadata·JSONL 로그·리포트 파일에 동일하게 기록         |
| 재개/복구        | node_status·last_error·카운터 + 체크포인터(thread_id)로 중단 지점부터 재개   |
| 동시 처리        | 누적 필드는 모두 reducer(merge_findings, merge_sources, merge_dict)로 병합  |
| 종료 보장        | step_count·rework_count·revision_count 상한 + 서브그래프 도구 호출 상한     |
"""

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 종료 보장 상한
# ---------------------------------------------------------------------------
MAX_STEPS = 12        # supervisor 결정 횟수 상한
MAX_REWORK = 2        # 같은 하위 에이전트에게 재작업을 요청할 수 있는 횟수
MAX_REVISIONS = 2     # 품질 평가 미달로 리포트를 다시 쓰는 횟수
MAX_TOOL_ROUNDS = 8   # 하위 에이전트 서브그래프의 도구 호출 라운드 상한
MAX_CALLS_PER_ROUND = 8  # 한 라운드에 병렬로 실행할 도구 호출 수 상한

Worker = Literal["researcher", "analyst", "writer"]

# 관점(perspective) → (표시명, 담당 에이전트, 근거로 인정하는 도구)
PERSPECTIVES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "overview": ("기업 개요", "researcher", ("get_company_overview",)),
    "financials": ("재무제표", "researcher", ("get_financial_statements",)),
    "price": ("주가 동향", "researcher", ("get_price_summary",)),
    "disclosures": ("최근 공시", "researcher", ("get_recent_disclosures",)),
    "business": ("사업 구조·리스크", "researcher", ("search_business_report",)),
    "ratios": ("재무 비율", "analyst", ("calculate_financial_ratios",)),
}
ANALYSIS_PERSPECTIVES = list(PERSPECTIVES)


# ---------------------------------------------------------------------------
# 구조화 산출물
# ---------------------------------------------------------------------------

class Source(TypedDict):
    """근거 출처 메타데이터 (REFERENCE·Groundedness·편향 통제에 사용). 원문은 저장하지 않는다."""
    id: str            # S1, S2 … 리포트 본문의 [S1] 인용 키
    key: str           # tool + args 정규화 키 (중복 제거용)
    tool: str
    company: str
    perspective: str
    provider: str      # DART / Yahoo Finance — 단일 출처 편중 검사용
    label: str
    url: str


class Finding(TypedDict):
    """하위 에이전트 1회 작업 결과."""
    agent: str
    content: str                    # 에이전트 최종 보고 (+ 재무제표 원본 표)
    source_ids: list[str]
    coverage: dict[str, list[str]]  # perspective → 근거를 확보한 회사 목록
    attempt: int


class TaskSpec(BaseModel):
    """supervisor의 요청 분석 결과."""
    task_type: Literal["analysis", "fact", "out_of_scope"] = Field(
        description="analysis: 기업 분석·비교 / fact: 단순 사실 질문 / out_of_scope: 해외 기업·재무 무관")
    companies: list[str] = Field(description="질문에 나온 국내 상장사 이름 (없으면 빈 목록)")
    required_perspectives: list[Literal["overview", "financials", "price", "disclosures", "business", "ratios"]] = Field(
        description="답변에 꼭 필요한 관점. analysis면 시스템이 전체 관점으로 채운다")
    reason: str = Field(description="판단 근거 한 문장")


class Sufficiency(TypedDict):
    """supervisor의 근거 충분도 평가 (규칙 기반)."""
    covered: list[str]
    missing: list[str]
    sufficient: bool


class Decision(TypedDict):
    """최신 라우팅 결정 1건. 전체 이력은 외부 로그에 남긴다."""
    next: str
    instruction: str
    reason: str
    override: str | None   # 규칙 가드가 LLM 결정을 바꾼 경우 그 사유


class CriterionResult(BaseModel):
    passed: bool
    score: int = Field(ge=1, le=5, description="1(매우 미흡) ~ 5(매우 우수)")
    issues: list[str] = Field(default_factory=list, description="구체적인 문제점. 없으면 빈 목록")


class JudgeVerdict(BaseModel):
    """LLM Judge 판정."""
    groundedness: CriterionResult = Field(description="모든 주장·수치가 근거 자료와 [S#] 출처로 추적되는가")
    neutrality: CriterionResult = Field(description="투자 권유·매수/매도 의견·근거 없는 우열 판정이 없는가")
    bias_control: CriterionResult = Field(description="단일 출처·유리한 근거에 편중되지 않고 강점과 리스크가 균형 있는가")
    fix_target: Literal["writer", "researcher", "analyst"] = Field(
        description="미달 시 고쳐야 할 주체. 근거 자체가 부족하면 researcher/analyst, 서술 문제면 writer")
    feedback: str = Field(description="수정 지시 (2~4문장)")


class EvalResult(TypedDict):
    passed: bool
    rule_checks: dict[str, bool]
    rule_issues: list[str]
    judge: dict | None
    fix_target: str
    feedback: str


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------

def merge_dict(left: dict | None, right: dict | None) -> dict:
    """키 단위 병합. right=None 이면 초기화(새 질문 시작)."""
    if right is None:
        return {}
    return {**(left or {}), **right}


def merge_findings(left: dict | None, right: dict | None) -> dict:
    """에이전트별 최신 결과만 유지 → 재작업이 반복돼도 State가 늘어나지 않는다."""
    return merge_dict(left, right)


def merge_sources(left: list | None, right: list | None) -> list:
    """key 기준 중복 제거 append. right=None 이면 초기화."""
    if right is None:
        return []
    left = left or []
    seen = {s["key"] for s in left}
    return left + [s for s in right if s["key"] not in seen and not seen.add(s["key"])]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class FinanceState(TypedDict, total=False):
    # ── 작업 페이로드 ───────────────────────────────────────────
    messages: Annotated[list[AnyMessage], add_messages]   # 사용자 질문 + 최종 리포트 (후속 질문용)
    question: str
    task: dict | None                                     # TaskSpec
    findings: Annotated[dict[str, Finding], merge_findings]
    sources: Annotated[list[Source], merge_sources]
    report: str
    eval_result: EvalResult | None

    # ── 제어 메타데이터 (조정·종료·재개에 필요한 최소치) ─────────────
    trace_id: str                                         # 외부 로그/트레이스 상관 키
    next: str                                             # supervisor → add_conditional_edges 분기 키
    instruction: str                                      # 다음 에이전트에게 줄 지시
    decision: Decision | None
    sufficiency: Sufficiency | None
    step_count: int
    rework_count: Annotated[dict[str, int], merge_dict]
    revision_count: int
    node_status: Annotated[dict[str, str], merge_dict]    # agent → running/done/failed
    last_error: str | None
