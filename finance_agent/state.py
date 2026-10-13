"""State Schema.

설계 원칙 (README "State Schema" 절과 1:1로 대응)
1. 제어 vs 페이로드 분리 : 아래 두 블록으로 나눈다. supervisor 라우팅은 '제어 메타'와
   관점별 충분성 플래그만 보고 결정한다.
2. 관측성 위치      : 라우팅 결정·사유 로그는 State에 넣지 않는다.
                       observability.log_decision()이 outputs/runs/{trace_id}/decisions.jsonl에 적재한다.
3. 지속성 비용      : 도구 원문(감사보고서 재무표, 문서 발췌 등)은 파일로 저장하고 State에는
                       EvidenceRef(ID·종류·제목·경로)만 둔다. messages는 사용자 질문과 최종 보고서만 담는다.
4. 상관            : trace_id 하나로 State ↔ 결정 로그 ↔ 근거 파일 ↔ LangSmith(run metadata)를 잇는다.
5. 재개/복구        : checkpointer + node_status + last_error + attempts 로 중단 지점부터 재개·재시도 판단.
6. 동시 처리        : 누적 필드는 reducer로 병합 (evidence: 이어붙이기, perspectives/node_status/attempts: dict 병합).
7. 종료 보장        : step_count/max_steps, 관점별 attempts, revision_count를 config.py 상수로 코드에서 강제.
"""

import operator
from typing import Annotated, Literal

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

PerspectiveKey = Literal["company_team", "technology", "market", "investment"]
WorkerName = Literal["researcher", "market_researcher", "investment_analyst"]
NextNode = Literal["researcher", "market_researcher", "investment_analyst", "writer", "evaluator",
                   "FINISH", "FINISH_WARNING"]
ScoreKey = Literal["founder", "market", "product_tech", "competitive_edge", "traction", "deal_terms"]


def merge_dict(left: dict | None, right: dict | None) -> dict:
    """dict 병합 reducer. 같은 key는 오른쪽(새 값)이 이긴다."""
    return {**(left or {}), **(right or {})}


# ── 구조화 산출물 ──────────────────────────────────────────────


class EvidenceRef(BaseModel):
    """근거 1건의 '참조'. 원문은 path 파일에 있고 State에는 이 메타데이터만 남긴다."""

    id: str                     # E1, E2, ... (보고서 인용 키)
    type: str                   # overview | disclosure | company_doc | industry_doc | financials | audit_notes
    provider: str               # 출처 기관 (편향 통제 검사용)
    tool: str
    title: str
    url: str | None = None
    path: str                   # outputs/runs/{trace_id}/evidence/E1.md
    collected_by: str


class PerspectiveResult(BaseModel):
    """관점별 분석 결과 (노션 샘플의 PerspectiveResult)."""

    view: str                                   # PerspectiveKey
    findings: str = ""
    evidence_ids: list[str] = Field(default_factory=list)   # 근거 추적 (REFERENCE·편향 통제 연결)
    gaps: list[str] = Field(default_factory=list)            # 부족한 점 (supervisor·evaluator가 기록)
    written_by: str | None = None
    sufficient: bool = False                    # supervisor 충분성 판정 결과
    sufficiency_reason: str = ""
    limited: bool = False                       # 재작업 한도 소진 → 보고서에 한계로 명시


class ScoreItem(BaseModel):
    key: str                                    # ScoreKey
    label: str
    weight: int
    score: int | None = None                    # 1~5, 근거 부족이면 None(결측)
    rationale: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class Scorecard(BaseModel):
    """투자 평가 스코어카드. LLM은 항목별 1~5점과 근거만 매기고, 가중합은 코드가 계산한다."""

    items: list[ScoreItem]
    total: float | None = None                  # 결측 제외 항목 가중 평균을 100점으로 환산
    covered_weight: int = 0                     # 점수가 매겨진 항목의 가중치 합 (최대 100)


class CriterionResult(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)


class EvalVerdict(BaseModel):
    """품질 평가 노드 결과 (규칙 검사 + LLM Judge)."""

    passed: bool
    groundedness: CriterionResult
    neutrality: CriterionResult
    bias_control: CriterionResult
    coverage: CriterionResult
    judge_used: bool                            # 규칙 검사 통과 시에만 LLM Judge 실행
    fix_target: Literal["writer", "researcher", "market_researcher", "investment_analyst"] | None = None
    rework_perspectives: list[str] = Field(default_factory=list)
    feedback: str = ""


# ── Graph State ────────────────────────────────────────────────


class FinanceState(MessagesState):
    # ── 작업 페이로드 ───────────────────────────────────────────
    question: str
    company: str
    perspectives: Annotated[dict[str, PerspectiveResult], merge_dict]
    evidence: Annotated[list[EvidenceRef], operator.add]
    scorecard: Scorecard | None                 # investment_analyst가 작성, writer가 표로 삽입
    report: str | None
    eval_result: EvalVerdict | None

    # ── 제어 메타데이터 (라우팅·종료·재개에 필요한 최소치) ───────
    trace_id: str                               # ★ 외부 로그·LangSmith와 잇는 상관 키
    next: NextNode | None                       # supervisor 결정 (conditional edge가 읽음)
    instruction: str                            # 다음 담당자에게 주는 지시
    target_perspectives: list[str]              # 이번 작업 대상 관점
    step_count: int                             # supervisor 판단 횟수
    max_steps: int
    attempts: Annotated[dict[str, int], merge_dict]      # 관점별 작업 횟수 (재작업 상한)
    revision_count: int                         # 품질 미달로 되돌린 횟수
    node_status: Annotated[dict[str, str], merge_dict]   # 노드별 마지막 상태 ok|error|skipped
    last_error: str | None
    status: Literal["running", "completed", "completed_with_warning", "out_of_scope"]
