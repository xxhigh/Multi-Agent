"""Researcher · Analyst 노드.

- 각자 ReAct 서브그래프로 도구를 쓰고, 끝나면 supervisor로만 돌아간다 (하위 에이전트 간 직접 통신 없음).
- 도구 원문은 근거 파일로 저장하고 State에는 EvidenceRef만 올린다 (지속성 비용 통제).
- 최종 보고는 관점별 구조화 결과(PerspectiveResult)로 정리해 State에 기록한다.
"""

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from .. import config, llm
from ..observability import load_evidence, log_decision, save_evidence
from ..prompts import ANALYST_PROMPT, RESEARCHER_PROMPT, STRUCTURING_PROMPT
from ..state import EvidenceRef, FinanceState, PerspectiveKey, PerspectiveResult
from ..tools import ANALYST_TOOLS, EVIDENCE_TYPE_BY_TOOL, RESEARCH_TOOLS
from . import _react

WORKER_SPECS = {
    "researcher": (RESEARCHER_PROMPT, RESEARCH_TOOLS),
    "analyst": (ANALYST_PROMPT, ANALYST_TOOLS),
}
_EXCERPT = 1200          # 하위 에이전트에게 넘기는 근거 발췌 길이
_MAX_FINDINGS = 1500     # 관점별 findings 최대 길이 (State 증식 방지)


class PerspectiveFinding(BaseModel):
    perspective: PerspectiveKey
    findings: str = Field(description="해당 관점의 사실·수치 정리. 없으면 빈 문자열")
    evidence_ids: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list, description="확보하지 못한 정보")


class WorkerFindings(BaseModel):
    items: list[PerspectiveFinding]


def _is_error(content: str) -> bool:
    head = content.strip()[:40]
    return head.startswith(("오류", "Error", "계산 오류")) or "찾을 수 없습니다" in head


def _evidence_catalog(evidence: list[EvidenceRef]) -> str:
    if not evidence:
        return "(아직 수집된 근거 없음)"
    return "\n".join(f"- {e.id} [{e.provider}] {e.title}" for e in evidence)


def render_worker_input(state: FinanceState, name: str, targets: list[str]) -> str:
    """하위 에이전트에게 줄 입력. 대화 전체가 아니라 필요한 것만 골라 넘긴다 (컨텍스트 격리)."""
    persp = state.get("perspectives") or {}
    lines = [
        f"분석 대상 기업: {state['company']}",
        f"사용자 요청: {state['question']}",
        f"supervisor 지시: {state.get('instruction') or '담당 관점의 근거를 수집·정리하세요.'}",
        "",
        "이번에 담당할 관점:",
    ]
    for key in targets:
        spec = config.PERSPECTIVES[key]
        prev = persp.get(key)
        lines.append(f"- {spec['title']}: {spec['guide']}")
        if prev and prev.gaps:
            lines.append(f"  · 이전 시도에서 부족했던 점: {'; '.join(prev.gaps[-3:])}")
        if prev and prev.findings:
            lines.append(f"  · 지금까지 정리된 내용(보강할 것): {prev.findings[:500]}")

    evidence = state.get("evidence") or []
    if evidence and name == "analyst":
        # analyst는 researcher가 모은 시세·재무제표를 해석해야 하므로 원문 발췌를 함께 준다
        lines += ["", "참고 근거 발췌 (researcher 수집):"]
        for e in evidence:
            if e.type in ("price", "statements", "overview"):
                lines.append(f"[{e.id}] {load_evidence(e.path, _EXCERPT)}")
    return "\n".join(lines)


def _merge(prev: PerspectiveResult | None, new: PerspectiveFinding, name: str,
           valid_ids: set[str]) -> PerspectiveResult:
    ids = [i for i in new.evidence_ids if i in valid_ids]
    findings = new.findings.strip()
    if prev and prev.findings and prev.written_by != name and findings:
        # 다른 담당자가 같은 관점에 보탠 내용은 합친다 (예: researcher 시세 + analyst 지표)
        findings = f"{prev.findings}\n{findings}"
        ids = list(dict.fromkeys(prev.evidence_ids + ids))
    elif prev and not findings:
        findings, ids = prev.findings, prev.evidence_ids
    return PerspectiveResult(
        view=new.perspective,
        findings=findings[:_MAX_FINDINGS],
        evidence_ids=ids,
        gaps=new.gaps,
        written_by=name,
    )


def make_worker_node(name: str):
    prompt, tools = WORKER_SPECS[name]
    agent = _react.build_react_agent(prompt, tools)

    def node(state: FinanceState) -> dict:
        trace_id = state["trace_id"]
        targets = state.get("target_perspectives") or [
            k for k, v in config.PERSPECTIVES.items() if v["owner"] == name
        ]
        try:
            result = agent.invoke({"messages": [HumanMessage(render_worker_input(state, name, targets))]})
        except Exception as e:  # noqa: BLE001 — 실패해도 그래프를 멈추지 않고 supervisor가 재시도 판단
            log_decision(trace_id, name, "error", f"{type(e).__name__}: {e}")
            return {"node_status": {name: "error"}, "last_error": f"{name}: {type(e).__name__}: {e}"}

        # 1) 도구 원문 → 근거 파일 저장, State엔 참조만
        offset = len(state.get("evidence") or [])
        new_refs: list[EvidenceRef] = []
        tool_errors: list[str] = []
        for m in result["messages"]:
            if not isinstance(m, ToolMessage):
                continue
            content = str(m.content)
            ev_type = EVIDENCE_TYPE_BY_TOOL.get(m.name or "")
            if _is_error(content):
                tool_errors.append(f"{m.name}: {content[:120]}")
                continue
            if ev_type:
                ref = save_evidence(trace_id, f"E{offset + len(new_refs) + 1}", ev_type, m.name, content, name)
                new_refs.append(EvidenceRef(**ref))

        # 2) 최종 보고 → 관점별 구조화 결과
        all_evidence = (state.get("evidence") or []) + new_refs
        valid_ids = {e.id for e in all_evidence}
        final_text = str(result["messages"][-1].content)
        targets_text = ", ".join(f"{k}({config.PERSPECTIVES[k]['title']})" for k in targets)
        try:
            parsed: WorkerFindings = llm.structured(
                WorkerFindings,
                [
                    SystemMessage(STRUCTURING_PROMPT.format(
                        worker=name, targets=targets_text, evidence_catalog=_evidence_catalog(all_evidence))),
                    HumanMessage(final_text),
                ],
                run_name=f"{name}_structuring",
            )
            items = [i for i in parsed.items if i.perspective in targets]
        except Exception as e:  # noqa: BLE001
            tool_errors.append(f"structuring: {e}")
            items = []

        prev = state.get("perspectives") or {}
        updates = {item.perspective: _merge(prev.get(item.perspective), item, name, valid_ids) for item in items}
        for key in targets:  # 보고에서 빠진 관점도 '시도했음'과 부족 사유를 남긴다
            if key not in updates:
                base = prev.get(key) or PerspectiveResult(view=key)
                updates[key] = base.model_copy(update={"gaps": [f"{name} 보고에 이 관점 내용이 없음"]})

        status = "ok" if not tool_errors else "partial"
        log_decision(trace_id, name, status, f"근거 {len(new_refs)}건 수집, 관점 {list(updates)} 갱신",
                     tool_errors=tool_errors[:5])
        return {
            "perspectives": updates,
            "evidence": new_refs,
            "node_status": {name: status},
            "last_error": tool_errors[-1] if tool_errors else None,
        }

    node.__name__ = name
    return node
