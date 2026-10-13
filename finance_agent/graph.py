"""Supervisor 기반 멀티 에이전트 그래프.

                    ┌──────────────────── supervisor ────────────────────┐
        add_conditional_edges(route_after_supervisor: State.next 기준)
        │               │                  │             │          │        │
   researcher   market_researcher   investment_analyst   writer   evaluator   END
        └───────────────┴──────────────────┴─────────────┴──────────┴──▶ supervisor (항상 복귀)

- 하위 에이전트끼리는 엣지가 없다 → 모든 통신은 supervisor를 거친다.
- 순서는 코드에 없다. supervisor가 State(관점별 충분성·평가 결과·상한)를 보고 next를 정하면
  route_after_supervisor()가 그 값만 읽어 분기한다.
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from . import config
from .agents.evaluator import evaluator_node
from .agents.workers import make_worker_node
from .agents.writer import writer_node
from .state import FinanceState
from .supervisor import supervisor_node

ROUTES = {
    **{name: name for name in config.WORKERS},
    "writer": "writer",
    "evaluator": "evaluator",
    "end": END,
}


def route_after_supervisor(state: FinanceState) -> str:
    """순수 함수 게이트: supervisor가 기록한 next만 보고 다음 노드를 고른다.

    supervisor 판단이 비정상이어도 그래프가 끝나도록 최종 안전망을 둔다.
    """
    nxt = state.get("next")
    if nxt in (None, "FINISH", "FINISH_WARNING"):
        return "end"
    if (state.get("step_count") or 0) > (state.get("max_steps") or config.MAX_STEPS) + 2:
        return "end"
    return nxt if nxt in ROUTES else "end"


def build_graph(checkpointer=None):
    g = StateGraph(FinanceState)
    g.add_node("supervisor", supervisor_node)
    for name in config.WORKERS:
        g.add_node(name, make_worker_node(name))
    g.add_node("writer", writer_node)
    g.add_node("evaluator", evaluator_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route_after_supervisor, ROUTES)
    for name in (*config.WORKERS, "writer", "evaluator"):
        g.add_edge(name, "supervisor")
    return g.compile(checkpointer=checkpointer or InMemorySaver())
