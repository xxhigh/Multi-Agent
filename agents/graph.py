"""Supervisor 기반 멀티 agent 재무 분석 그래프.

                              ┌─Send─▶ researcher ───┐
 START → init → supervisor ───┼─Send─▶ web_searcher ─┼─▶ supervisor → … → writer → supervisor → END
                              └─Send─▶ analyst ──────┘

- init       : 질문마다 제어 메타(run_id, step, rounds …)와 누적 필드를 초기화
- supervisor : 매 턴 증거가 충분한지 판단하고 다음 작업을 결정. 서로 독립인 작업은 Send로 동시에 내보낸다.
               부족한 관점이 있으면 researcher/web_searcher/analyst에게 그 관점만 재조사시킨다.
- researcher : 기업 개요·주가·재무제표 등 원천 데이터 수집 (도구 사용)
- web_searcher : 뉴스·업계 동향 등 DART에 없는 외부 정보를 Tavily 웹 검색으로 수집 (도구 사용)
- analyst    : 재무 비율 계산 및 해석 (도구 사용)
- writer     : 수집·분석 결과를 한국어 리포트로 작성 (증거가 충분하거나 조사 한도에 도달했을 때만)

State 스키마와 설계 원칙은 state.py 참고.
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

from prompts import ANALYST_PROMPT, RESEARCHER_PROMPT, SUPERVISOR_PROMPT, WEB_SEARCHER_PROMPT, WRITER_PROMPT

from . import trace
from .state import Decision, ErrorRecord, FinanceState, Finding, Task, WorkerInput
from .tools import ANALYST_TOOLS, FINANCIAL_TABLE_MARK, RESEARCH_TOOLS, WEB_TOOLS

# 종료 보장: 모든 반복에 상한을 둔다
MAX_STEPS = 10  # 질문 하나에 supervisor가 내릴 수 있는 결정 수. 마지막 결정은 writer로 고정
MAX_RESEARCH_ROUNDS = 6  # 질문 하나에 researcher/web_searcher/analyst에게 맡길 수 있는 작업 수
MAX_PARALLEL = 3  # 한 번에 동시에 내보내는 작업 수
MAX_ATTEMPTS = 2  # 같은 작업의 시도 횟수 (최초 1회 + 재시도 1회)
MAX_HISTORY_MESSAGES = 7  # 대화에 남기는 메시지 수 (이전 질문·리포트 3쌍 + 이번 질문)


class Assignment(BaseModel):
    agent: Literal["researcher", "web_searcher", "analyst", "writer"] = Field(description="작업할 agent")
    instruction: str = Field(description="그 agent에게 줄 구체적인 작업 지시")


class Route(BaseModel):
    """supervisor의 증거 충분성 판단과 라우팅 결정."""

    sufficient: bool = Field(description="지금까지의 결과만으로 사용자 질문에 답할 증거가 충분한가")
    missing: list[str] = Field(description="근거가 부족한 관점 이름 목록. 충분하면 빈 목록")
    reason: str = Field(description="이렇게 판단하고 이 agent를 고른 이유 (1~2문장)")
    assignments: list[Assignment] = Field(
        description="다음에 맡길 작업. 서로 독립인 작업은 여러 개를 넣어 동시에 실행할 수 있다. "
                    "writer는 sufficient가 true일 때만, 단독으로 선택"
    )


def _llm() -> ChatOpenAI:
    return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-5-mini"))


def _make_tool_agent(system_prompt: str, tools: list, max_tool_rounds: int = 8):
    """LLM ↔ ToolNode 루프를 도는 단일 ReAct agent 서브그래프.

    도구 호출이 max_tool_rounds 회를 넘으면 도구 없이 최종 답변을 강제해 무한 루프를 막는다.
    """
    base = _llm()
    llm = base.bind_tools(tools)

    def call_model(state: MessagesState):
        msgs = [SystemMessage(system_prompt.format(today=date.today()))] + state["messages"]
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
    # 도구 호출 기록(사업보고서 본문 등)은 체크포인트에 저장하지 않는다. 중단되면 이 worker만 처음부터 다시 실행된다.
    return g.compile(checkpointer=False)


def _context(messages: list, findings: list[Finding]) -> list:
    """LLM에 보여 줄 대화: 이전 질문·리포트와 이번 질문, 그 뒤로 이번 질문의 작업 지시와 결과."""
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


def _init_node(state: FinanceState, config: RunnableConfig) -> dict:
    """새 질문의 실행을 시작한다. 중단된 실행을 재개할 때는 이 노드를 거치지 않으므로 진행 상태가 유지된다."""
    run_id = uuid.uuid4().hex[:12]
    messages = state["messages"]
    trace.log(run_id, "run_start", thread_id=config["configurable"].get("thread_id"), question=messages[-1].content)
    return {
        "run_id": run_id, "status": "running", "step": 0, "rounds": 0, "decision": None,
        "findings": None, "errors": None, "report": "",  # 이전 질문의 결과는 비운다 (리포트는 messages에 남아 있다)
        "messages": [RemoveMessage(id=m.id) for m in messages[:-MAX_HISTORY_MESSAGES]],
    }


def _worker_node(name: str, agent):
    """서브그래프를 실행하고 최종 답변만 findings에 남긴다.

    재무제표 도구의 원본 표는 요약 과정에서 숫자가 바뀌지 않도록 최종 답변 뒤에 그대로 붙인다.
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
        tables = [m.content for m in result["messages"][len(state["messages"]):]
                  if isinstance(m, ToolMessage) and FINANCIAL_TABLE_MARK in str(m.content)]
        if tables:
            content += "\n\n" + "\n\n".join(tables)
        trace.log(state["run_id"], "worker_done", step=state["step"], agent=name, attempt=task["attempt"],
                  instruction=task["instruction"], content=content)
        return {"findings": [Finding(agent=name, instruction=task["instruction"], content=content, step=state["step"])]}

    return node


def _writer_node(state: WorkerInput) -> dict:
    try:
        msgs = [SystemMessage(WRITER_PROMPT.format(today=date.today()))] + state["messages"]
        report = _llm().invoke(msgs).content
        # 원본 표에 붙은 내부 안내 문구는 리포트에 노출하지 않는다
        report = "\n".join(line for line in report.splitlines() if FINANCIAL_TABLE_MARK not in line)
        if not report.strip():
            raise ValueError("빈 리포트")
    except Exception as e:  # noqa: BLE001 - LLM·네트워크 오류 등
        return _failure(state, e)
    trace.log(state["run_id"], "worker_done", step=state["step"], agent="writer", attempt=state["task"]["attempt"],
              instruction=state["task"]["instruction"], content=report)
    return {"report": report, "messages": [AIMessage(content=report, name="writer")]}


def _plan(decision: Route, state: FinanceState) -> tuple[list[Task], list[str], str]:
    """LLM의 결정을 규칙에 맞게 다듬는다: (내보낼 작업, 부족한 관점, 사유)."""
    missing = [] if decision.sufficient else decision.missing
    reason = decision.reason
    tasks = [Task(agent=a.agent, instruction=a.instruction, attempt=1) for a in decision.assignments]
    research = [t for t in tasks if t["agent"] != "writer"]
    writer = next((t for t in tasks if t["agent"] == "writer"), None)
    budget = MAX_RESEARCH_ROUNDS - state["rounds"]
    if state["step"] + 1 >= MAX_STEPS:
        budget = 0

    if budget <= 0:
        # 조사 한도: 루프를 끊고 지금까지의 결과로 리포트를 쓰게 한다
        if research or not writer:
            reason += " → 조사 한도에 도달해 writer로 진행"
        tasks = [writer or Task(agent="writer", attempt=1,
                                instruction="조사 한도에 도달했습니다. 지금까지의 결과만으로 리포트를 작성하세요.")]
    elif research and (missing or not writer):
        tasks = research[:min(budget, MAX_PARALLEL)]
    elif missing:
        # 증거가 부족한데 writer만 고른 경우: 부족한 관점을 먼저 보강한다
        reason += " → 근거가 부족해 writer를 보류하고 재조사"
        tasks = [Task(agent="researcher", attempt=1,
                      instruction=f"다음 관점의 근거를 추가로 수집하세요: {', '.join(missing)}")]
    else:
        tasks = [writer or Task(agent="writer", attempt=1, instruction="지금까지의 결과로 리포트를 작성하세요.")]

    if tasks[0]["agent"] == "writer" and missing:
        tasks[0]["instruction"] += (f" 근거를 확보하지 못한 관점({', '.join(missing)})은 "
                                    "추정하지 말고 확인되지 않았다고 밝히세요.")
    return tasks, missing, reason


def _dispatch(state: FinanceState, findings: list[Finding], tasks: list[Task], sufficient: bool,
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
        "rounds": state["rounds"] + sum(1 for t in tasks if t["agent"] != "writer"),
        "decision": decision,
        "findings": notes,
    })


def _finish(state: FinanceState, status: str) -> Command:
    trace.log(state["run_id"], "run_end", step=state["step"], status=status)
    return Command(goto=END, update={"status": status})


def _supervisor_node(
    state: FinanceState,
) -> Command[Literal["researcher", "web_searcher", "analyst", "writer", "__end__"]]:
    if state["report"]:  # 리포트가 나왔으면 종료
        return _finish(state, "done")

    # 직전 결정에서 실패한 작업: 시도 횟수가 남았으면 그대로 다시 맡기고, 아니면 실패로 확정한다
    failed = [e for e in state["errors"] if e["step"] == state["step"]]
    retry = [Task(agent=e["agent"], instruction=e["instruction"], attempt=e["attempt"] + 1)
             for e in failed if e["attempt"] < MAX_ATTEMPTS]
    gave_up = [e for e in failed if e["attempt"] >= MAX_ATTEMPTS]
    if any(e["agent"] == "writer" for e in gave_up):
        return _finish(state, "failed")
    notes = [Finding(agent=e["agent"], instruction=e["instruction"], step=e["step"],
                     content=f"오류로 이 작업을 완료하지 못했습니다 ({e['error']}). 같은 작업을 다시 요청하지 마세요.")
             for e in gave_up]
    findings = state["findings"] + notes
    if retry:
        last = state["decision"]
        return _dispatch(state, findings, retry, last["sufficient"], last["missing"],
                         "직전 작업이 오류로 실패해 다시 시도", notes)

    router = _llm().with_structured_output(Route)
    decision: Route = router.invoke([SystemMessage(SUPERVISOR_PROMPT)] + _context(state["messages"], findings))
    tasks, missing, reason = _plan(decision, state)
    return _dispatch(state, findings, tasks, not missing, missing, reason, notes)


def build_graph(checkpointer=None):
    researcher = _make_tool_agent(RESEARCHER_PROMPT, RESEARCH_TOOLS)
    web_searcher = _make_tool_agent(WEB_SEARCHER_PROMPT, WEB_TOOLS)
    analyst = _make_tool_agent(ANALYST_PROMPT, ANALYST_TOOLS)

    g = StateGraph(FinanceState)
    g.add_node("init", _init_node)
    g.add_node("supervisor", _supervisor_node)
    g.add_node("researcher", _worker_node("researcher", researcher))
    g.add_node("web_searcher", _worker_node("web_searcher", web_searcher))
    g.add_node("analyst", _worker_node("analyst", analyst))
    g.add_node("writer", _writer_node)
    g.add_edge(START, "init")
    g.add_edge("init", "supervisor")
    for worker in ("researcher", "web_searcher", "analyst", "writer"):
        g.add_edge(worker, "supervisor")
    return g.compile(checkpointer=checkpointer or InMemorySaver())
