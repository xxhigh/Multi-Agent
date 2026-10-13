"""하위 에이전트용 ReAct 서브그래프 (LLM ↔ ToolNode 루프)."""

from datetime import date

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from .. import config, llm


def build_react_agent(system_prompt: str, tools: list, max_tool_rounds: int = config.MAX_TOOL_ROUNDS):
    """도구 호출이 max_tool_rounds 회를 넘으면 도구 없이 최종 답변을 강제해 무한 루프를 막는다."""
    base = llm.chat_model()
    with_tools = base.bind_tools(tools)

    def call_model(state: MessagesState):
        msgs = [SystemMessage(system_prompt.format(today=date.today()))] + state["messages"]
        rounds = sum(1 for m in state["messages"] if isinstance(m, AIMessage) and m.tool_calls)
        if rounds >= max_tool_rounds:
            msgs.append(HumanMessage("도구 호출 한도에 도달했습니다. 지금까지 얻은 결과만으로 최종 보고를 작성하세요."))
            return {"messages": [base.invoke(msgs)]}
        return {"messages": [with_tools.invoke(msgs)]}

    g = StateGraph(MessagesState)
    g.add_node("agent", call_model)
    # 도구 예외도 그래프 전체를 멈추지 않고 LLM에게 오류 메시지로 돌려준다
    g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile()
