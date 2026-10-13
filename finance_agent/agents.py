"""하위 에이전트 (researcher · analyst · writer).

- 하위 에이전트는 supervisor가 State에 남긴 instruction만 보고 일하고, 결과를 State에 쓴 뒤 supervisor로 돌아간다.
  서로를 직접 호출하지 않는다 (그래프 엣지가 모두 → supervisor).
- researcher / analyst 는 LLM ↔ ToolNode ReAct 서브그래프. 도구 원문은 서브그래프 안에서만 쓰고
  메인 State에는 최종 보고(Finding)와 출처 메타데이터(Source)만 남긴다.
"""

import os
import re
from datetime import date

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from . import dart
from .observability import log_decision
from .prompts import WRITER_PROMPT
from .state import MAX_CALLS_PER_ROUND, MAX_TOOL_ROUNDS, PERSPECTIVES, FinanceState, Finding, Source
from .tools import FINANCIAL_TABLE_MARK

TOOL_TO_PERSPECTIVE = {tool: key for key, (_, _, tools) in PERSPECTIVES.items() for tool in tools}
_URL = re.compile(r"https?://[^\s)\]]+")


def llm(model_env: str = "OPENAI_MODEL") -> ChatOpenAI:
    return ChatOpenAI(model=os.getenv(model_env) or os.getenv("OPENAI_MODEL", "gpt-5-mini"))


# ---------------------------------------------------------------------------
# ReAct 서브그래프
# ---------------------------------------------------------------------------

def make_tool_agent(system_prompt: str, tools: list, max_tool_rounds: int = MAX_TOOL_ROUNDS):
    """LLM ↔ ToolNode 루프. 도구 호출이 상한을 넘으면 도구 없이 최종 답변을 강제한다."""
    base = llm()
    bound = base.bind_tools(tools)

    def call_model(state: MessagesState):
        msgs = [SystemMessage(system_prompt.format(today=date.today()))] + state["messages"]
        rounds = sum(1 for m in state["messages"] if isinstance(m, AIMessage) and m.tool_calls)
        if rounds >= max_tool_rounds:
            msgs.append(HumanMessage("도구 호출 한도에 도달했습니다. 지금까지 얻은 결과만으로 최종 보고를 작성하세요."))
            return {"messages": [base.invoke(msgs)]}
        resp = bound.invoke(msgs)
        if len(resp.tool_calls) > MAX_CALLS_PER_ROUND:  # 한 번에 수십 개씩 병렬 호출하는 것을 막는다
            resp.tool_calls = resp.tool_calls[:MAX_CALLS_PER_ROUND]
        return {"messages": [resp]}

    g = StateGraph(MessagesState)
    g.add_node("agent", call_model)
    g.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile()


# ---------------------------------------------------------------------------
# 출처 추출
# ---------------------------------------------------------------------------

def _company(args: dict) -> str:
    raw = str(args.get("company", "")).strip()
    try:
        return dart.resolve_corp(raw).name
    except Exception:  # noqa: BLE001 - 정규화 실패 시 원래 이름 사용
        return raw


def _source(tool: str, args: dict, content: str) -> dict | None:
    perspective = TOOL_TO_PERSPECTIVE.get(tool)
    if perspective is None or content.startswith(("오류", "Error")) or "찾을 수 없습니다" in content[:120]:
        return None
    company = _company(args)
    url = (_URL.findall(content) or [None])[0]
    if tool == "get_financial_statements":
        report = args.get("report", "annual")
        key, label, provider = f"{tool}:{company}:{report}:{args.get('year')}", \
            f"OpenDART 재무제표 API — {company} ({report})", "DART"
    elif tool == "search_business_report":
        key, label, provider = f"{tool}:{company}", f"DART 사업보고서 원문 — {company}", "DART"
    elif tool == "get_recent_disclosures":
        key, label, provider = f"{tool}:{company}", f"DART 최근 공시 목록 — {company}", "DART"
        url = f"https://dart.fss.or.kr/dsab007/main.do?textCrpNm={company}"
    elif tool == "get_company_overview":
        key, label, provider = f"{tool}:{company}", f"OpenDART 기업개황 (+ Yahoo Finance 시가총액) — {company}", \
            "DART·Yahoo Finance"
    elif tool == "get_price_summary":
        key, label, provider = f"{tool}:{company}:{args.get('period', '1y')}", \
            f"Yahoo Finance 주가 이력 — {company} ({args.get('period', '1y')})", "Yahoo Finance"
        url = "https://finance.yahoo.com"
    else:  # calculate_financial_ratios
        key, label, provider = f"{tool}:{company}", \
            f"재무 비율 계산 (DART 사업보고서 + Yahoo Finance 시가총액) — {company}", "DART·Yahoo Finance"
    return {"key": key, "tool": tool, "company": company, "perspective": perspective,
            "provider": provider, "label": label, "url": url or "https://opendart.fss.or.kr"}


def _collect(new_msgs: list, existing: list[Source]) -> tuple[list[Source], list[str], dict, list[str]]:
    """서브그래프 메시지에서 (새 출처, 이번 작업이 쓴 출처 id, coverage, 원본 표) 추출."""
    calls = {c["id"]: c for m in new_msgs if isinstance(m, AIMessage) for c in m.tool_calls}
    by_key = {s["key"]: s for s in existing}
    new, used, coverage, tables = [], [], {}, []
    for m in new_msgs:
        if not isinstance(m, ToolMessage) or m.tool_call_id not in calls:
            continue
        call, content = calls[m.tool_call_id], str(m.content)
        src = None if m.status == "error" else _source(call["name"], call["args"], content)
        if src is None:
            continue
        if src["key"] in by_key:
            sid = by_key[src["key"]]["id"]
        else:
            sid = f"S{len(by_key) + 1}"
            src = Source(id=sid, **src)
            by_key[src["key"]] = src
            new.append(src)
        used.append(sid)
        companies = coverage.setdefault(src["perspective"], [])
        if src["company"] not in companies:
            companies.append(src["company"])
        if FINANCIAL_TABLE_MARK in content:
            tables.append(f"[{sid}] {content}")
    return new, list(dict.fromkeys(used)), coverage, tables


# ---------------------------------------------------------------------------
# 하위 에이전트 노드
# ---------------------------------------------------------------------------

def _context(state: FinanceState, name: str) -> list:
    msgs = [HumanMessage(f"사용자 질문: {state['question']}")]
    for agent, f in (state.get("findings") or {}).items():
        who = "당신의 이전 보고" if agent == name else f"{agent} 보고"
        msgs.append(HumanMessage(f"[{who}]\n{f['content']}"))
    msgs.append(HumanMessage(f"[supervisor 지시] {state.get('instruction', '')}"))
    return msgs


def worker_node(name: str, agent):
    def node(state: FinanceState) -> dict:
        trace_id = state["trace_id"]
        prev: Finding | None = (state.get("findings") or {}).get(name)
        inputs = _context(state, name)
        try:
            result = agent.invoke({"messages": inputs}, {"run_name": name, "metadata": {"trace_id": trace_id}})
        except Exception as e:  # noqa: BLE001 - 실패를 State에 남기고 supervisor가 재시도/제외 결정
            log_decision(trace_id, name, "failed", error=f"{type(e).__name__}: {e}")
            return {"node_status": {name: "failed"}, "last_error": f"{name}: {type(e).__name__}: {e}"}

        new_msgs = result["messages"][len(inputs):]
        new_sources, used, coverage, tables = _collect(new_msgs, state.get("sources") or [])
        content = result["messages"][-1].content
        if tables:
            content += "\n\n" + "\n\n".join(tables)
        if used:
            content += "\n\n[이번 보고의 근거 출처] " + ", ".join(used)

        if prev:  # 재작업이면 이전 결과에 보완분을 덧붙인다 (덮어써도 근거가 사라지지 않게)
            content = f"{prev['content']}\n\n[보완 {prev['attempt']}차]\n{content}"
            for p, cs in prev["coverage"].items():
                coverage[p] = list(dict.fromkeys(cs + coverage.get(p, [])))
            used = list(dict.fromkeys(prev["source_ids"] + used))

        finding = Finding(agent=name, content=content, source_ids=used, coverage=coverage,
                          attempt=(prev["attempt"] + 1) if prev else 1)
        log_decision(trace_id, name, "done", attempt=finding["attempt"],
                     coverage=coverage, new_sources=[s["id"] for s in new_sources])
        return {"findings": {name: finding}, "sources": new_sources,
                "node_status": {name: "done"}, "last_error": None}

    return node


def _sources_table(sources: list[Source]) -> str:
    return "\n".join(f"[{s['id']}] {s['label']} | 관점: {PERSPECTIVES[s['perspective']][0]} | "
                     f"제공: {s['provider']} | {s['url']}" for s in sources) or "(수집된 출처 없음)"


def writer_node(state: FinanceState) -> dict:
    trace_id = state["trace_id"]
    findings = state.get("findings") or {}
    task = state.get("task") or {}
    parts = [f"사용자 질문: {state['question']}",
             f"요청 유형: {task.get('task_type', 'analysis')}"]
    for agent in ("researcher", "analyst"):
        if agent in findings:
            parts.append(f"=== {agent} 결과 ===\n{findings[agent]['content']}")
    missing = (state.get("sufficiency") or {}).get("missing") or []
    if missing:
        parts.append("확보하지 못한 관점(리포트에 '데이터 없음'으로 명시): " + ", ".join(missing))
    parts.append("=== 출처 목록 (본문 인용은 [S#] 형식) ===\n" + _sources_table(state.get("sources") or []))
    ev = state.get("eval_result")
    if ev and not ev["passed"] and state.get("report"):
        parts.append(f"=== 이전 초안 ===\n{state['report']}")
        parts.append("=== 품질 평가 피드백 (반드시 반영) ===\n"
                     + "\n".join(f"- {i}" for i in ev["rule_issues"]) + f"\n{ev['feedback']}")
    parts.append(f"[supervisor 지시] {state.get('instruction', '')}")

    msgs = [SystemMessage(WRITER_PROMPT.format(today=date.today())), HumanMessage("\n\n".join(parts))]
    report = llm().invoke(msgs, {"run_name": "writer", "metadata": {"trace_id": trace_id}}).content
    # 원본 표 머리에 붙은 내부 안내 문구는 리포트에 노출하지 않는다
    report = "\n".join(line for line in report.splitlines() if FINANCIAL_TABLE_MARK not in line)
    log_decision(trace_id, "writer", "done", chars=len(report), revision=state.get("revision_count", 0))
    return {"report": report, "node_status": {"writer": "done"}}
