"""그래프 State 스키마.

설계 원칙
- 제어 메타(라우팅·종료·복구에 쓰는 작은 값)와 페이로드(작업 결과)를 필드로 나눈다.
  supervisor의 조정 로직은 제어 메타만 읽고, 페이로드는 LLM에 보여 줄 때만 쓴다.
- State에는 "지금 실행을 이어가는 데 필요한 것"만 둔다. 결정 이력과 사유는 trace(JSONL)로 빼고
  run_id + step 으로 잇는다. State에는 마지막 결정 하나만 남는다.
- 누적 필드는 질문이 시작될 때 비운다(findings, errors). 대화(messages)는 질문·리포트만 담고 길이를 제한한다.
  worker 내부의 도구 호출 기록은 State에 들어오지 않는다.
- 병렬로 실행되는 worker가 쓰는 필드(findings, errors, messages)에는 reducer가 있다.
  나머지 필드는 한 superstep에 한 노드만 쓴다(init, supervisor, 단독 실행되는 writer).
"""

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class Task(TypedDict):
    """supervisor가 worker 한 명에게 맡긴 작업."""

    agent: str
    instruction: str
    attempt: int  # 1부터. 실패해서 다시 맡기면 증가


class Decision(TypedDict):
    """supervisor의 결정 한 건. 전체 이력은 trace에 있고 State에는 마지막 것만 둔다."""

    step: int
    sufficient: bool
    missing: list[str]  # 근거가 부족하다고 본 관점
    reason: str
    tasks: list[Task]  # 이 결정으로 내보낸 작업 (중단 시 무엇을 하던 중이었는지)


class Finding(TypedDict):
    """worker의 작업 결과."""

    agent: str
    instruction: str
    content: str
    step: int


class ErrorRecord(TypedDict):
    """실패한 작업. supervisor가 재시도 여부를 정하는 데 쓴다."""

    agent: str
    instruction: str
    step: int
    attempt: int
    error: str


def extend(current: list | None, update: list | None) -> list:
    """병렬 worker의 동시 쓰기를 이어 붙이는 reducer. update가 None이면 비운다(새 질문 시작)."""
    if update is None:
        return []
    return (current or []) + update


class FinanceState(TypedDict):
    # ── 페이로드: 작업 결과 ────────────────────────────────────────────────
    messages: Annotated[list[AnyMessage], add_messages]  # 사용자 질문과 최종 리포트 (후속 질문의 맥락)
    findings: Annotated[list[Finding], extend]  # 이번 질문에서 worker가 낸 결과
    report: str  # 이번 질문의 최종 리포트. 비어 있지 않으면 종료

    # ── 제어 메타: 조정·종료·복구 ──────────────────────────────────────────
    run_id: str  # 질문 한 건의 실행 ID. trace 파일과 잇는 키
    status: Literal["running", "done", "failed"]
    step: int  # supervisor 결정 횟수 (상한 MAX_STEPS)
    rounds: int  # researcher/web_searcher/analyst에게 맡긴 작업 수 (상한 MAX_RESEARCH_ROUNDS)
    decision: Decision | None  # 마지막 결정
    errors: Annotated[list[ErrorRecord], extend]  # 이번 질문에서 실패한 작업


class WorkerInput(TypedDict):
    """Send로 worker에 넘기는 입력. worker는 전체 State가 아니라 이것만 본다."""

    messages: list[AnyMessage]  # 대화 + 지금까지의 결과 + 이번 지시
    task: Task
    step: int
    run_id: str
