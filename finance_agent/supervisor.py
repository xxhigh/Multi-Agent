"""조정 계층: supervisor.

매 턴 State(요청 유형, 확보 관점, 근거 충분도, 재작업·수정 횟수, 품질 평가 결과)를 읽고
다음 작업자를 State["next"]에 기록한다. 실제 분기는 graph.py의 add_conditional_edges가 이 값으로 수행한다.

결정 = LLM 판단 + 규칙 가드 (Hybrid)
1. LLM(Route)이 근거 충분 여부와 다음 작업자를 제안한다.
2. 규칙 가드가 State를 기준으로 제안을 검증한다.
   - 필수 관점이 비었는데 writer로 가려 하면 → 담당 에이전트에게 재작업 지시
   - 재작업 상한에 도달한 에이전트는 다시 부르지 않고 → 다음 단계로 진행(누락은 리포트에 명시)
   - 리포트 없이 FINISH 하려 하면 → writer
   - 품질 평가 미달 뒤 재수집을 한 번 했으면 → 다시 writer (재수집만 반복하지 않게)
   - 결정 횟수 상한(MAX_STEPS)에 도달하면 → 강제 마무리
"""

from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from .agents import llm
from .observability import log_decision
from .prompts import SUPERVISOR_PROMPT, TASK_PROMPT
from .state import (ANALYSIS_PERSPECTIVES, MAX_REVISIONS, MAX_REWORK, MAX_STEPS, PERSPECTIVES, Decision,
                    FinanceState, Sufficiency, TaskSpec)

DIGEST_FINDING_CHARS = 1500


class Route(BaseModel):
    """supervisor의 라우팅 결정."""
    evidence_sufficient: bool = Field(description="현재 근거만으로 질문에 답하는 리포트를 쓸 수 있는가")
    next: Literal["researcher", "analyst", "writer", "FINISH"] = Field(description="다음 작업자")
    instruction: str = Field(description="선택한 작업자에게 줄 구체적 지시 1~2문장 (FINISH면 빈 문자열)")
    reason: str = Field(description="이 결정을 내린 근거 한 문장")


# ---------------------------------------------------------------------------
# 근거 충분도 (규칙 기반)
# ---------------------------------------------------------------------------

def assess_sufficiency(state: FinanceState, task: dict) -> Sufficiency:
    """필수 관점마다 대상 기업 수만큼 근거(성공한 도구 호출)가 확보됐는지 본다."""
    coverage: dict[str, set[str]] = {}
    for f in (state.get("findings") or {}).values():
        for p, companies in f["coverage"].items():
            coverage.setdefault(p, set()).update(companies)
    need = max(1, len(task.get("companies") or []))
    covered, missing = [], []
    for p in task["required_perspectives"]:
        (covered if len(coverage.get(p, ())) >= need else missing).append(p)
    return Sufficiency(covered=covered, missing=missing, sufficient=not missing)


def _owner(perspective: str) -> str:
    return PERSPECTIVES[perspective][1]


def _names(keys: list[str]) -> str:
    return ", ".join(PERSPECTIVES[k][0] for k in keys) or "없음"


def _ran(state: FinanceState, agent: str) -> bool:
    return agent in (state.get("findings") or {}) or (state.get("node_status") or {}).get(agent) == "failed"


def _can_rework(state: FinanceState, agent: str) -> bool:
    return not _ran(state, agent) or (state.get("rework_count") or {}).get(agent, 0) < MAX_REWORK


# ---------------------------------------------------------------------------
# State 요약 → LLM 입력
# ---------------------------------------------------------------------------

def _digest(state: FinanceState, task: dict, suff: Sufficiency) -> str:
    status = state.get("node_status") or {}
    rework = state.get("rework_count") or {}
    lines = [
        f"사용자 질문: {state['question']}",
        f"요청 유형: {task['task_type']} / 대상 기업: {', '.join(task['companies']) or '없음'}",
        f"필수 관점: {_names(task['required_perspectives'])}",
        f"확보한 관점: {_names(suff['covered'])}",
        f"누락된 관점: {_names(suff['missing'])} "
        f"(담당: {', '.join(sorted({_owner(p) for p in suff['missing']})) or '-'})",
        "에이전트 상태: " + ", ".join(
            f"{a}={status.get(a, '미실행')}(재작업 {rework.get(a, 0)}/{MAX_REWORK})" for a in ("researcher", "analyst")),
        f"리포트: {'있음 (' + str(len(state['report'])) + '자, 수정 ' + str(state.get('revision_count', 0)) + '/' + str(MAX_REVISIONS) + ')' if state.get('report') else '없음'}",
    ]
    if state.get("last_error"):
        lines.append(f"최근 오류: {state['last_error']}")
    ev = state.get("eval_result")
    if ev and not ev["passed"]:
        lines.append(f"품질 평가: 미달 / 수정 주체 제안: {ev['fix_target']}\n"
                     + "\n".join(f"  - {i}" for i in ev["rule_issues"]) + f"\n  피드백: {ev['feedback']}")
    for agent, f in (state.get("findings") or {}).items():
        lines.append(f"\n[{agent} 결과 요약]\n{f['content'][:DIGEST_FINDING_CHARS]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 규칙 가드
# ---------------------------------------------------------------------------

def _guard(state: FinanceState, task: dict, suff: Sufficiency, nxt: str, instruction: str):
    """(next, instruction, override 사유 | None)"""
    report, ev = state.get("report"), state.get("eval_result")

    if (state.get("step_count") or 0) >= MAX_STEPS:
        return ("FINISH", "", "결정 횟수 상한 도달 → 강제 마무리") if report else \
            ("writer", "지금까지의 근거만으로 리포트를 작성하세요.", "결정 횟수 상한 도달 → 리포트 강제 작성")

    if task["task_type"] == "out_of_scope":
        return ("FINISH", "", None) if report else \
            ("writer", "국내 상장사 재무 분석만 지원한다고 안내하세요.", None if nxt == "writer" else "지원 범위 밖 요청 → 안내")

    last = state.get("decision") or {}
    if report and ev and not ev["passed"] and last.get("next") in ("researcher", "analyst") \
            and nxt in ("researcher", "analyst"):
        return "writer", "보완된 근거로 품질 평가 피드백을 반영해 리포트를 수정하세요.", \
            "평가 후 재수집 1회 완료 → 재작성"

    if nxt == "FINISH" and not report:
        nxt, instruction = "writer", "확보한 근거로 리포트를 작성하세요."
        override = "리포트 없이 종료 불가 → writer"
    elif nxt == "FINISH" and ev and not ev["passed"] and state.get("revision_count", 0) < MAX_REVISIONS:
        nxt, instruction = ev["fix_target"], ev["feedback"]
        override = "품질 평가 미달 상태로 종료 불가 → 수정"
    else:
        override = None

    if nxt == "writer" and not suff["sufficient"]:
        for p in suff["missing"]:
            owner = _owner(p)
            if _can_rework(state, owner):
                miss = [m for m in suff["missing"] if _owner(m) == owner]
                return owner, (f"근거가 부족한 관점을 보완하세요: {_names(miss)}. "
                               f"대상: {', '.join(task['companies']) or '질문의 기업'}."), \
                    f"필수 관점 누락({_names(miss)}) → {owner} 재작업"

    if nxt in ("researcher", "analyst") and not _can_rework(state, nxt):
        alt = next((_owner(p) for p in suff["missing"] if _can_rework(state, _owner(p))), "writer")
        return alt, ("확보한 근거로 리포트를 작성하고, 누락 관점은 '데이터 없음'으로 명시하세요."
                     if alt == "writer" else instruction), f"{nxt} 재작업 상한 도달 → {alt}로 진행(fallback)"

    return nxt, instruction, override


# ---------------------------------------------------------------------------
# 노드
# ---------------------------------------------------------------------------

def _plan_task(state: FinanceState) -> dict:
    spec: TaskSpec = llm().with_structured_output(TaskSpec).invoke(
        [SystemMessage(TASK_PROMPT), HumanMessage(state["question"])],
        {"run_name": "supervisor.task", "metadata": {"trace_id": state["trace_id"]}})
    task = spec.model_dump()
    if task["task_type"] == "analysis":
        task["required_perspectives"] = list(ANALYSIS_PERSPECTIVES)
    elif task["task_type"] == "out_of_scope":
        task["required_perspectives"] = []
    elif not task["required_perspectives"]:
        task["required_perspectives"] = ["financials"]
    log_decision(state["trace_id"], "supervisor", "task", **task)
    return task


def supervisor_node(state: FinanceState) -> dict:
    trace_id = state["trace_id"]
    task = state.get("task") or _plan_task(state)
    suff = assess_sufficiency(state, task)

    route: Route | None = None
    if task["task_type"] != "out_of_scope" and (state.get("step_count") or 0) < MAX_STEPS:
        route = llm().with_structured_output(Route).invoke(
            [SystemMessage(SUPERVISOR_PROMPT), HumanMessage(_digest(state, task, suff))],
            {"run_name": "supervisor.route", "metadata": {"trace_id": trace_id}})
    proposed = route.next if route else "writer"
    nxt, instruction, override = _guard(state, task, suff, proposed, route.instruction if route else "")

    updates: dict = {"task": task, "sufficiency": suff, "next": nxt, "instruction": instruction,
                     "step_count": (state.get("step_count") or 0) + 1}
    if nxt in ("researcher", "analyst") and _ran(state, nxt):
        updates["rework_count"] = {nxt: (state.get("rework_count") or {}).get(nxt, 0) + 1}
    if nxt == "writer" and state.get("report"):
        updates["revision_count"] = state.get("revision_count", 0) + 1

    reason = route.reason if route else "규칙 기반 결정"
    updates["decision"] = Decision(next=nxt, instruction=instruction, reason=reason, override=override)
    log_decision(trace_id, "supervisor", "route", step=updates["step_count"], proposed=proposed, next=nxt,
                 reason=reason, override=override, llm_sufficient=route.evidence_sufficient if route else None,
                 covered=suff["covered"], missing=suff["missing"],
                 rework=updates.get("rework_count"), revision=updates.get("revision_count"))
    return updates


def route_from_supervisor(state: FinanceState) -> str:
    """add_conditional_edges 분기 함수. 순서를 하드코딩하지 않고 supervisor가 State에 쓴 결정만 따른다."""
    return state["next"]
