"""Supervisor 패턴 재무 분석 그래프.

            ┌─────────────── supervisor ◀──────────────────┐
            │   (add_conditional_edges: State["next"])     │
   ┌────────┼──────────┬──────────┐                        │
researcher analyst   writer     FINISH                     │
   │        │          │          │                        │
   └────────┴──▶ supervisor       │                        │
                       ▼          │                        │
                   evaluator ── 미달(수정 여력 있음) ────────┘
                       │ 통과 / 수정 한도
                       ▼          ▼
                     finalize ◀───┘ ──▶ END

- 하위 에이전트끼리는 엣지가 없다. 모든 결과는 supervisor로만 돌아간다.
- supervisor는 근거 충분도(필수 관점 확보 여부)를 평가한 뒤에만 writer를 고른다.
- writer 뒤에는 항상 evaluator가 붙고, 미달이면 supervisor로 돌아가 재작성/재수집 Loop를 돈다.
"""

import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .agents import make_tool_agent, worker_node, writer_node
from .evaluator import evaluator_node, finalize_node, route_after_eval
from .observability import log_decision
from .prompts import ANALYST_PROMPT, RESEARCHER_PROMPT
from .state import FinanceState
from .supervisor import route_from_supervisor, supervisor_node
from .tools import ANALYST_TOOLS, RESEARCH_TOOLS


def prepare_node(state: FinanceState) -> dict:
    """새 질문마다 실행 단위(run) 상태를 초기화한다. 대화 기록(messages)만 이어진다."""
    question = state["messages"][-1].content
    trace_id = state.get("trace_id") or uuid.uuid4().hex  # 호출 측이 run마다 새로 넣어 준다
    log_decision(trace_id, "prepare", "start", question=question)
    return {
        "question": question, "trace_id": trace_id,
        "task": None, "findings": None, "sources": None, "report": "", "eval_result": None,
        "next": "", "instruction": "", "decision": None, "sufficiency": None,
        "step_count": 0, "rework_count": None, "revision_count": 0, "node_status": None, "last_error": None,
    }


def build_graph(checkpointer=None):
    g = StateGraph(FinanceState)
    g.add_node("prepare", prepare_node)
    g.add_node("supervisor", supervisor_node)
    g.add_node("researcher", worker_node("researcher", make_tool_agent(RESEARCHER_PROMPT, RESEARCH_TOOLS)))
    g.add_node("analyst", worker_node("analyst", make_tool_agent(ANALYST_PROMPT, ANALYST_TOOLS)))
    g.add_node("writer", writer_node)
    g.add_node("evaluator", evaluator_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "prepare")
    g.add_edge("prepare", "supervisor")
    g.add_conditional_edges("supervisor", route_from_supervisor, {
        "researcher": "researcher",
        "analyst": "analyst",
        "writer": "writer",
        "FINISH": "finalize",
    })
    g.add_edge("researcher", "supervisor")
    g.add_edge("analyst", "supervisor")
    g.add_edge("writer", "evaluator")
    g.add_conditional_edges("evaluator", route_after_eval, {"supervisor": "supervisor", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
