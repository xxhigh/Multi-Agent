"""Supervisor 기반 멀티 agent 스타트업 투자 평가 그래프.

                              ┌─Send─▶ company_researcher ─┐
 START → init → supervisor ───┼─Send─▶ tech_researcher ────┼─▶ supervisor → evaluator → supervisor
                              └─Send─▶ market_researcher ──┘        → writer ⇄ quality → supervisor → END

- init       : 질문마다 제어 메타(run_id, step, rounds …)와 누적 필드를 초기화
- supervisor : 매 턴 증거가 충분한지 판단하고 다음 작업을 결정. 서로 독립인 작업은 Send로 동시에 내보낸다.
               근거가 부족한 평가 항목이 있으면 해당 researcher에게 그 항목만 재조사시킨다.
- company_researcher : 기업 정보 수집 — 웹 검색, 국내 상장사는 DART (도구 사용)
- tech_researcher    : 기술 근거 수집 — 문서 풀 RAG, 웹 검색 (도구 사용)
- market_researcher  : 시장·경쟁 근거 수집 — 문서 풀 RAG, 웹 검색 (도구 사용)
- evaluator  : 평가 항목별 점수와 근거, 스코어카드 산출 (도구 사용)
- writer     : 조사·평가 결과를 투자 보고서로 작성 (증거가 충분하거나 조사 한도에 도달했을 때만)
- quality    : 보고서 품질 평가 (Groundedness · 중립성 · 편향 통제 · 관점 커버리지). 미달이면 writer로 되돌린다

State 스키마와 설계 원칙은 state.py, 평가 항목은 criteria.py 참고.
"""

import os
import uuid
from datetime import date
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, Send
from pydantic import BaseModel, Field

from prompts import (
    COMPANY_RESEARCHER_PROMPT,
    EVALUATOR_PROMPT,
    MARKET_RESEARCHER_PROMPT,
    SUPERVISOR_PROMPT,
    TECH_RESEARCHER_PROMPT,
    WRITER_PROMPT,
)

from . import criteria, quality, report, trace
from .state import Decision, ErrorRecord, EvaluationState, Finding, Task, WorkerInput
from .tools import COMPANY_TOOLS, EVALUATOR_TOOLS, MARKET_TOOLS, TECH_TOOLS, VERBATIM_MARK

RESEARCHERS = ("company_researcher", "tech_researcher", "market_researcher")

# 도구를 쓰는 worker: 이름 → (프롬프트, 도구)
WORKERS = {
    "company_researcher": (COMPANY_RESEARCHER_PROMPT, COMPANY_TOOLS),
    "tech_researcher": (TECH_RESEARCHER_PROMPT, TECH_TOOLS),
    "market_researcher": (MARKET_RESEARCHER_PROMPT, MARKET_TOOLS),
    "evaluator": (EVALUATOR_PROMPT, EVALUATOR_TOOLS),
}

# 종료 보장: 모든 반복에 상한을 둔다
MAX_STEPS = 10  # 질문 하나에 supervisor가 내릴 수 있는 결정 수. 마지막 결정은 writer로 고정
MAX_RESEARCH_ROUNDS = 5  # 질문 하나에 researcher에게 맡길 수 있는 작업 수 (기본 3건 + 재조사 2건)
MAX_PARALLEL = 3  # 한 번에 동시에 내보내는 작업 수
MAX_ATTEMPTS = 2  # 같은 작업의 시도 횟수 (최초 1회 + 재시도 1회)
MAX_QUALITY_ROUNDS = 2  # 품질 평가 미달로 보고서를 다시 쓰게 하는 횟수
MAX_HISTORY_MESSAGES = 7  # 대화에 남기는 메시지 수 (이전 질문·보고서 3쌍 + 이번 질문)


# supervisor가 근거 부족을 표시하는 단위. 구하려는 문서가 아니라 평가 항목으로 적게 해서 재조사 범위를 좁힌다
MISSING_LABELS = {**{c.key: c.name for c in criteria.CRITERIA},
                  "risk": "리스크", "scorecard": "스코어카드", "other": "요청한 사실"}


class Assignment(BaseModel):
    agent: Literal["company_researcher", "tech_researcher", "market_researcher", "evaluator", "writer"] = Field(
        description="작업할 agent")
    instruction: str = Field(description="그 agent에게 줄 구체적인 작업 지시")


class Route(BaseModel):
    """supervisor의 증거 충분성 판단과 라우팅 결정."""

    sufficient: bool = Field(description="지금까지의 결과만으로 사용자 질문에 답할 증거가 충분한가")
    missing: list[Literal[tuple(MISSING_LABELS)]] = Field(
        description="근거가 부족한 평가 항목 key 목록. 리스크는 risk, 스코어카드가 없으면 scorecard, "
                    "단순 질문에서 요청한 사실이 없으면 other. 충분하면 빈 목록")
    needs_scorecard: bool = Field(
        description="투자 평가·검토·비교 요청이라 스코어카드가 필요하면 true. 단순 사실 질문이나 지원 범위 밖 요청이면 false")
    reason: str = Field(description="이렇게 판단하고 이 agent를 고른 이유 (1~2문장)")
    assignments: list[Assignment] = Field(
        description="다음에 맡길 작업. 서로 독립인 작업은 여러 개를 넣어 동시에 실행할 수 있다. "
                    "writer는 sufficient가 true일 때만, 단독으로 선택"
    )


def _llm() -> ChatOpenAI:
    return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-5-mini"))


def _render(prompt: str) -> str:
    return prompt.format(today=date.today(), criteria=criteria.render())


def _make_tool_agent(system_prompt: str, tools: list, max_tool_rounds: int = 8):
    """LLM ↔ ToolNode 루프를 도는 단일 ReAct agent 서브그래프.

    도구 호출이 max_tool_rounds 회를 넘으면 도구 없이 최종 답변을 강제해 무한 루프를 막는다.
    """
    base = _llm()
    llm = base.bind_tools(tools)

    def call_model(state: MessagesState):
        msgs = [SystemMessage(_render(system_prompt))] + state["messages"]
        rounds = sum(1 for m in state["messages"] if isinstance(m, AIMessage) and m.tool_calls)
        if rounds >= max_tool_rounds:
            msgs.append(HumanMessage("도구 호출 한도에 도달했습니다. 지금까지 얻은 결과만으로 최종 보고를 작성하세요."))
            return {"messages": [base.invoke(msgs)]}
        return {"messages": [llm.invoke(msgs)]}

    g = StateGraph(MessagesState)
    g.add_node("agent", call_model)
    # 예상 못 한 도구 예외도 그래프 전체를 멈추지 않고 LLM에게 오류 메시지로 돌려준다
    g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    # 도구 호출 기록(검색 결과 본문 등)은 체크포인트에 저장하지 않는다. 중단되면 이 worker만 처음부터 다시 실행된다.
    return g.compile(checkpointer=False)


def _context(messages: list, findings: list[Finding]) -> list:
    """LLM에 보여 줄 대화: 이전 질문·보고서와 이번 질문, 그 뒤로 이번 질문의 작업 지시와 결과."""
    out = list(messages)
    for f in findings:
        out.append(HumanMessage(content=f"[{f['agent']}에게 지시] {f['instruction']}", name="supervisor"))
        out.append(HumanMessage(content=f["content"], name=f["agent"]))
    return out


def _failure(state: WorkerInput, error: Exception) -> dict:
    """작업 실패를 예외로 터뜨리지 않고 State에 남겨 supervisor가 재시도 여부를 정하게 한다."""
    task = state["task"]
    record = ErrorRecord(agent=task["agent"], instruction=task["instruction"], step=state["step"],
                         attempt=task["attempt"], error=f"{type(error).__name__}: {error}")
    trace.log(state["run_id"], "worker_error", **record)
    return {"errors": [record]}


def _init_node(state: EvaluationState, config: RunnableConfig) -> dict:
    """새 질문의 실행을 시작한다. 중단된 실행을 재개할 때는 이 노드를 거치지 않으므로 진행 상태가 유지된다."""
    run_id = uuid.uuid4().hex[:12]
    messages = state["messages"]
    trace.log(run_id, "run_start", thread_id=config["configurable"].get("thread_id"), question=messages[-1].content)
    return {
        "run_id": run_id, "status": "running", "step": 0, "rounds": 0, "decision": None,
        "quality": None, "quality_rounds": 0,
        "findings": None, "errors": None, "report": "",  # 이전 질문의 결과는 비운다 (보고서는 messages에 남아 있다)
        "messages": [RemoveMessage(id=m.id) for m in messages[:-MAX_HISTORY_MESSAGES]],
    }


def _worker_node(name: str, agent):
    """서브그래프를 실행하고 최종 답변만 findings에 남긴다.

    재무제표·스코어카드처럼 원본 표시가 있는 도구 결과는 요약 과정에서 숫자가 바뀌지 않도록 최종 답변 뒤에 그대로 붙인다.
    """

    def node(state: WorkerInput) -> dict:
        task = state["task"]
        try:
            result = agent.invoke({"messages": state["messages"]})
        except GraphBubbleUp:
            raise
        except Exception as e:  # noqa: BLE001 - LLM·네트워크 오류 등
            return _failure(state, e)
        content = result["messages"][-1].content
        tool_results = [str(m.content) for m in result["messages"][len(state["messages"]):] if isinstance(m, ToolMessage)]
        tables = [t for t in tool_results if VERBATIM_MARK in t]
        if tables:
            content += "\n\n" + "\n\n".join(tables)
        trace.log(state["run_id"], "worker_done", step=state["step"], agent=name, attempt=task["attempt"],
                  instruction=task["instruction"], content=content)
        sources = sorted({u for t in tool_results for u in quality.urls(t)})
        return {"findings": [Finding(agent=name, instruction=task["instruction"], content=content, step=state["step"],
                                     sources=sources)]}

    return node


def _writer_node(state: WorkerInput) -> Command[Literal["quality", "supervisor"]]:
    """보고서를 작성해 품질 평가로 넘긴다. 형식(SUMMARY·목차·REFERENCE 표기)을 검사해 어긋나면 한 번 고쳐 쓰게 한다."""
    try:
        llm = _llm()
        msgs = [SystemMessage(_render(WRITER_PROMPT))] + state["messages"]
        # 스코어카드가 있으면 투자 평가 요청이므로 목차 전체를 지켜야 한다
        full = any(VERBATIM_MARK in str(m.content) and "스코어카드" in str(m.content) for m in state["messages"])
        text = report.normalize_references(llm.invoke(msgs).content)
        problems = report.violations(text, full)
        if problems:
            trace.log(state["run_id"], "report_format", step=state["step"], repaired=False, violations=problems)
            fix = ("보고서가 형식 규칙을 어겼습니다. 내용과 수치는 그대로 두고 아래 사항만 고쳐 보고서 전체를 다시 출력하세요.\n- "
                   + "\n- ".join(problems))
            text = report.normalize_references(
                llm.invoke(msgs + [AIMessage(content=text), HumanMessage(content=fix)]).content)
            problems = report.violations(text, full)
            trace.log(state["run_id"], "report_format", step=state["step"], repaired=True, violations=problems)
        # 원본 표에 붙은 내부 안내 문구는 보고서에 노출하지 않는다
        text = "\n".join(line for line in text.splitlines() if VERBATIM_MARK not in line)
        if not text.strip():
            raise ValueError("빈 보고서")
    except Exception as e:  # noqa: BLE001 - LLM·네트워크 오류 등
        return Command(goto="supervisor", update=_failure(state, e))
    trace.log(state["run_id"], "worker_done", step=state["step"], agent="writer", attempt=state["task"]["attempt"],
              instruction=state["task"]["instruction"], content=text)
    return Command(goto="quality", update={"report": text})


def _quality_node(state: EvaluationState) -> Command[Literal["writer", "supervisor"]]:
    """보고서 품질을 항목별 부합 여부로 판정한다. 미달이면 사유를 붙여 writer에게 다시 쓰게 한다 (횟수 제한)."""
    findings = state["findings"]
    full = any(VERBATIM_MARK in f["content"] and "스코어카드" in f["content"] for f in findings)
    retrieved = {u for f in findings for u in f.get("sources", [])} | set(report.pool_references())
    # REFERENCE 를 본문 인용에 맞춰 정리한 뒤 판정한다 (기계적으로 맞출 수 있는 것은 다시 쓰게 하지 않는다)
    text = quality.tidy(state["report"], "\n".join(f["content"] for f in findings), retrieved)
    result = quality.evaluate(text, full, retrieved)
    rounds = state["quality_rounds"]
    retry = not result["passed"] and rounds < MAX_QUALITY_ROUNDS
    trace.log(state["run_id"], "quality", step=state["step"], round=rounds, rewrite=retry, **result)
    if not retry:
        return Command(goto="supervisor", update={"quality": result, "report": text})

    task = next((t for t in state["decision"]["tasks"] if t["agent"] == "writer"),
                Task(agent="writer", instruction="보고서를 작성하세요.", attempt=1))
    feedback = ("보고서가 품질 평가에서 다음 항목에 미달했습니다. 대화에 있는 조사 결과만 사용해 아래 사항을 고친 보고서 전체를 "
                "다시 출력하세요. 지적되지 않은 부분과 수치·표는 그대로 둡니다.\n- " + "\n- ".join(result["issues"]))
    messages = _context(state["messages"], findings) + [
        HumanMessage(content=f"[writer에게 지시] {task['instruction']}", name="supervisor"),
        AIMessage(content=text),
        HumanMessage(content=feedback),
    ]
    return Command(
        goto=[Send("writer", WorkerInput(messages=messages, task=task, step=state["step"], run_id=state["run_id"]))],
        update={"quality": result, "quality_rounds": rounds + 1, "report": text},
    )


def _plan(decision: Route, state: EvaluationState, findings: list[Finding]) -> tuple[list[Task], list[str], str]:
    """LLM의 결정을 규칙에 맞게 다듬는다: (내보낼 작업, 부족한 항목, 사유).

    - 조사는 MAX_RESEARCH_ROUNDS 건까지, 결정은 MAX_STEPS 번까지. 마지막 결정은 writer다.
    - 스코어카드가 필요한 요청은 writer로 가기 전에 evaluator를 반드시 한 번 거친다.
    """
    missing = [] if decision.sufficient else [MISSING_LABELS[m] for m in dict.fromkeys(decision.missing)]
    reason = decision.reason
    tasks = [Task(agent=a.agent, instruction=a.instruction, attempt=1) for a in decision.assignments]
    research = [t for t in tasks if t["agent"] in RESEARCHERS]
    evaluator = next((t for t in tasks if t["agent"] == "evaluator"), None)
    writer = next((t for t in tasks if t["agent"] == "writer"), None)

    has_scorecard = any(f["agent"] == "evaluator" for f in findings)
    need_scorecard = decision.needs_scorecard and not has_scorecard
    remaining = MAX_STEPS - state["step"]  # 이번을 포함해 남은 결정 수
    budget = MAX_RESEARCH_ROUNDS - state["rounds"]
    # 조사를 더 내보내도 되는가: 조사 건수가 남아 있고, 뒤에 evaluator와 writer가 설 자리가 남아 있을 때만
    can_research = budget > 0 and remaining > 2

    if remaining <= 1:
        if not writer or research or evaluator:
            reason += " → 결정 한도에 도달해 writer로 진행"
        tasks = [writer or Task(agent="writer", attempt=1,
                                instruction="조사 한도에 도달했습니다. 지금까지의 결과만으로 보고서를 작성하세요.")]
    elif research and can_research and (missing or not (writer or evaluator)):
        tasks = research[:min(budget, MAX_PARALLEL)]
    elif evaluator and not writer:
        tasks = [evaluator]
    elif need_scorecard:
        # 스코어카드 없이 보고서로 넘어가려는 경우(조사 한도 도달 포함): 지금까지의 근거로 먼저 평가한다
        reason += " → 스코어카드가 없어 evaluator를 먼저 실행"
        tasks = [evaluator or Task(agent="evaluator", attempt=1,
                                   instruction="지금까지 수집된 근거로 평가 항목별 점수를 매기고 스코어카드를 산출하세요. "
                                               "근거가 없는 항목은 평가 보류로 두세요.")]
    elif missing and can_research and not has_scorecard:
        # 증거가 부족한데 writer만 고른 경우: 부족한 항목을 먼저 보강한다
        reason += " → 근거가 부족해 writer를 보류하고 재조사"
        tasks = [Task(agent="company_researcher", attempt=1,
                      instruction=f"다음 항목의 근거를 추가로 수집하세요: {', '.join(missing)}")]
    else:
        if research and not can_research:
            reason += " → 조사 한도에 도달해 writer로 진행"
        tasks = [writer or Task(agent="writer", attempt=1, instruction="지금까지의 결과로 보고서를 작성하세요.")]

    if tasks[0]["agent"] == "writer" and not decision.needs_scorecard:
        tasks[0]["instruction"] += " 투자 평가 요청이 아니므로 보고서 목차를 쓰지 말고 질문에 짧게 답한 뒤 REFERENCE 만 붙이세요."
    unresolved = [m for m in missing if m != MISSING_LABELS["scorecard"]]
    if tasks[0]["agent"] == "writer" and unresolved:
        tasks[0]["instruction"] += (f" 근거가 부족한 항목({', '.join(unresolved)})은 "
                                    "추정하지 말고 확인되지 않은 부분을 밝히세요.")
    return tasks, missing, reason


def _dispatch(state: EvaluationState, findings: list[Finding], tasks: list[Task], sufficient: bool,
              missing: list[str], reason: str, notes: list[Finding]) -> Command:
    """결정을 기록하고 작업을 내보낸다. 작업이 여러 개면 같은 superstep에서 동시에 실행된다."""
    step = state["step"] + 1
    decision = Decision(step=step, sufficient=sufficient, missing=missing, reason=reason, tasks=tasks)
    trace.log(state["run_id"], "decision", **decision)
    context = _context(state["messages"], findings)
    sends = [
        Send(t["agent"], WorkerInput(
            messages=context + [HumanMessage(content=f"[{t['agent']}에게 지시] {t['instruction']}", name="supervisor")],
            task=t, step=step, run_id=state["run_id"]))
        for t in tasks
    ]
    return Command(goto=sends, update={
        "step": step,
        "rounds": state["rounds"] + sum(1 for t in tasks if t["agent"] in RESEARCHERS),
        "decision": decision,
        "findings": notes,
    })


def _finish(state: EvaluationState, status: str) -> Command:
    trace.log(state["run_id"], "run_end", step=state["step"], status=status)
    update = {"status": status}
    if status == "done":  # 확정된 보고서만 대화에 남긴다 (다시 쓰는 동안의 초안은 남기지 않는다)
        update["messages"] = [AIMessage(content=state["report"], name="writer")]
    return Command(goto=END, update=update)


def _supervisor_node(
    state: EvaluationState,
) -> Command[Literal["company_researcher", "tech_researcher", "market_researcher", "evaluator", "writer", "__end__"]]:
    if state["report"]:  # 품질 평가까지 마친 보고서가 있으면 종료
        return _finish(state, "done")

    # 직전 결정에서 실패한 작업: 시도 횟수가 남았으면 그대로 다시 맡기고, 아니면 실패로 확정한다
    failed = [e for e in state["errors"] if e["step"] == state["step"]]
    retry = [Task(agent=e["agent"], instruction=e["instruction"], attempt=e["attempt"] + 1)
             for e in failed if e["attempt"] < MAX_ATTEMPTS]
    gave_up = [e for e in failed if e["attempt"] >= MAX_ATTEMPTS]
    if any(e["agent"] == "writer" for e in gave_up):
        return _finish(state, "failed")
    notes = [Finding(agent=e["agent"], instruction=e["instruction"], step=e["step"], sources=[],
                     content=f"오류로 이 작업을 완료하지 못했습니다 ({e['error']}). 같은 작업을 다시 요청하지 마세요.")
             for e in gave_up]
    findings = state["findings"] + notes
    if retry:
        last = state["decision"]
        return _dispatch(state, findings, retry, last["sufficient"], last["missing"],
                         "직전 작업이 오류로 실패해 다시 시도", notes)

    router = _llm().with_structured_output(Route)
    decision: Route = router.invoke([SystemMessage(_render(SUPERVISOR_PROMPT))] + _context(state["messages"], findings))
    tasks, missing, reason = _plan(decision, state, findings)
    return _dispatch(state, findings, tasks, not missing, missing, reason, notes)


def build_graph(checkpointer=None):
    g = StateGraph(EvaluationState)
    g.add_node("init", _init_node)
    g.add_node("supervisor", _supervisor_node)
    for name, (prompt, tools) in WORKERS.items():
        g.add_node(name, _worker_node(name, _make_tool_agent(prompt, tools)))
    g.add_node("writer", _writer_node)  # 성공하면 quality, 실패하면 supervisor로 간다
    g.add_node("quality", _quality_node)
    g.add_edge(START, "init")
    g.add_edge("init", "supervisor")
    for worker in WORKERS:
        g.add_edge(worker, "supervisor")
    return g.compile(checkpointer=checkpointer or InMemorySaver())
