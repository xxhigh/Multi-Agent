"""Supervisor 기반 멀티 agent 재무 분석 그래프.

    ┌──────────── supervisor ────────────┐
    │        │           │               │
 researcher  analyst    writer          END
    └────────┴───────────┴──▶ supervisor

- supervisor : 대화 내용을 보고 다음에 일할 agent를 결정
- researcher : 기업 개요·주가·재무제표 등 원천 데이터 수집 (도구 사용)
- analyst    : 재무 비율 계산 및 해석 (도구 사용)
- writer     : 수집·분석 결과를 한국어 리포트로 작성
"""

import os
from datetime import date
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command
from pydantic import BaseModel, Field

from prompts import ANALYST_PROMPT, RESEARCHER_PROMPT, SUPERVISOR_PROMPT, WRITER_PROMPT

from .tools import ANALYST_TOOLS, FINANCIAL_TABLE_MARK, RESEARCH_TOOLS

WORKERS = ("researcher", "analyst", "writer")


class FinanceState(MessagesState):
    next: str


class Route(BaseModel):
    """supervisor의 라우팅 결정."""

    next: Literal["researcher", "analyst", "writer", "FINISH"] = Field(
        description="다음에 작업할 agent. 모든 작업이 끝났으면 FINISH"
    )
    instruction: str = Field(description="선택한 agent에게 줄 구체적인 작업 지시 (FINISH면 빈 문자열)")


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
    return g.compile()


def _worker_node(name: str, agent):
    """서브그래프를 실행하고 최종 답변만 메인 대화에 남긴 뒤 supervisor로 복귀.

    재무제표 도구의 원본 표는 요약 과정에서 숫자가 바뀌지 않도록 최종 답변 뒤에 그대로 붙인다.
    """

    def node(state: FinanceState) -> Command[Literal["supervisor"]]:
        result = agent.invoke({"messages": state["messages"]})
        content = result["messages"][-1].content
        tables = [m.content for m in result["messages"][len(state["messages"]):]
                  if isinstance(m, ToolMessage) and FINANCIAL_TABLE_MARK in str(m.content)]
        if tables:
            content += "\n\n" + "\n\n".join(tables)
        return Command(
            goto="supervisor",
            update={"messages": [HumanMessage(content=content, name=name)]},
        )

    return node


def _writer_node(state: FinanceState) -> Command[Literal["supervisor"]]:
    msgs = [SystemMessage(WRITER_PROMPT.format(today=date.today()))] + state["messages"]
    report = _llm().invoke(msgs).content
    # 원본 표에 붙은 내부 안내 문구는 리포트에 노출하지 않는다
    report = "\n".join(line for line in report.splitlines() if FINANCIAL_TABLE_MARK not in line)
    return Command(goto="supervisor", update={"messages": [HumanMessage(content=report, name="writer")]})


def _supervisor_node(state: FinanceState) -> Command[Literal["researcher", "analyst", "writer", "__end__"]]:
    router = _llm().with_structured_output(Route)
    decision: Route = router.invoke([SystemMessage(SUPERVISOR_PROMPT)] + state["messages"])
    if decision.next == "FINISH":
        return Command(goto=END, update={"next": "FINISH"})
    return Command(
        goto=decision.next,
        update={
            "next": decision.next,
            "messages": [HumanMessage(content=f"[{decision.next}에게 지시] {decision.instruction}", name="supervisor")],
        },
    )


def build_graph(checkpointer=None):
    researcher = _make_tool_agent(RESEARCHER_PROMPT, RESEARCH_TOOLS)
    analyst = _make_tool_agent(ANALYST_PROMPT, ANALYST_TOOLS)

    g = StateGraph(FinanceState)
    g.add_node("supervisor", _supervisor_node)
    g.add_node("researcher", _worker_node("researcher", researcher))
    g.add_node("analyst", _worker_node("analyst", analyst))
    g.add_node("writer", _writer_node)
    g.add_edge(START, "supervisor")
    return g.compile(checkpointer=checkpointer or InMemorySaver())
