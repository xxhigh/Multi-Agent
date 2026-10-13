"""Writer 노드: 관점별 결과 + 근거로 보고서 작성. REFERENCE는 코드가 인용 ID로 생성한다."""

import re
from datetime import date

from langchain_core.messages import HumanMessage, SystemMessage

from .. import config, llm
from ..observability import load_evidence, log_decision
from ..prompts import REVISION_PROMPT, WRITER_PROMPT
from ..state import EvidenceRef, FinanceState, PerspectiveResult
from ..tools import FINANCIAL_TABLE_MARK

DISCLAIMER = "본 보고서는 정보 제공 목적이며 투자 권유가 아닙니다."
CITATION = re.compile(r"\[E(\d+)\]")
_EXCERPT = 900


def _perspective_block(key: str, p: PerspectiveResult | None) -> str:
    spec = config.PERSPECTIVES[key]
    if p is None or not p.findings:
        body = "(확보된 내용 없음)"
    else:
        body = p.findings
    flag = " [한계: 재작업 한도까지 근거를 충분히 확보하지 못함]" if (p and p.limited) or p is None else ""
    ids = ", ".join(p.evidence_ids) if p and p.evidence_ids else "-"
    gaps = f"\n부족한 점: {'; '.join(p.gaps)}" if p and p.gaps else ""
    return f"### {spec['title']} (노션 관점: {spec['notion_view']}){flag}\n근거 ID: {ids}\n{body}{gaps}"


def build_reference(report: str, evidence: list[EvidenceRef]) -> str:
    """본문에 실제로 인용된 근거만 REFERENCE로 만든다 (인용 ↔ 출처 1:1 연결)."""
    by_id = {e.id: e for e in evidence}
    cited = list(dict.fromkeys(f"E{n}" for n in CITATION.findall(report)))
    lines = ["## REFERENCE"]
    for eid in cited:
        e = by_id.get(eid)
        if e is None:
            continue
        url = f" — {e.url}" if e.url else ""
        lines.append(f"- [{e.id}] {e.provider} · {e.title}{url}")
    if len(lines) == 1:
        lines.append("- (인용된 근거 없음)")
    return "\n".join(lines)


def finalize_markdown(body: str, evidence: list[EvidenceRef]) -> str:
    """모델 출력 정리: 내부 표식 제거, 모델이 쓴 REFERENCE·면책 문구 제거 후 코드로 다시 붙임."""
    lines = [ln for ln in body.splitlines() if FINANCIAL_TABLE_MARK not in ln]
    text = "\n".join(lines)
    text = re.split(r"^##\s*REFERENCE.*$", text, flags=re.M)[0]
    text = text.replace(DISCLAIMER, "").rstrip()
    return f"{text}\n\n{build_reference(text, evidence)}\n\n{DISCLAIMER}\n"


def writer_node(state: FinanceState) -> dict:
    trace_id = state["trace_id"]
    evidence = state.get("evidence") or []
    perspectives = state.get("perspectives") or {}

    catalog = []
    tables = []
    for e in evidence:
        if e.type == "statements":
            tables.append(f"[{e.id}]\n{load_evidence(e.path)}")
        else:
            catalog.append(f"[{e.id}] ({e.provider}) {e.title}\n{load_evidence(e.path, _EXCERPT)}")

    user = "\n\n".join([
        f"사용자 요청: {state['question']}",
        "## 관점별 분석 결과\n" + "\n\n".join(_perspective_block(k, perspectives.get(k)) for k in config.PERSPECTIVE_KEYS),
        "## 근거 목록 (발췌)\n" + ("\n\n".join(catalog) or "(없음)"),
        "## 재무제표 원본 표\n" + ("\n\n".join(tables) or "(수집된 재무제표 없음 — 이 절에는 '수집되지 않음'이라고 적으세요)"),
    ])
    messages = [SystemMessage(WRITER_PROMPT.format(today=date.today(), company=state["company"])),
                HumanMessage(user)]

    verdict = state.get("eval_result")
    revising = verdict is not None and not verdict.passed and state.get("report")
    if revising:
        messages.append(HumanMessage(REVISION_PROMPT.format(feedback=verdict.feedback, previous=state["report"])))

    try:
        body = llm.text(messages, run_name="writer_revision" if revising else "writer")
    except Exception as e:  # noqa: BLE001
        log_decision(trace_id, "writer", "error", f"{type(e).__name__}: {e}")
        return {"node_status": {"writer": "error"}, "last_error": f"writer: {e}"}

    report = finalize_markdown(body, evidence)
    log_decision(trace_id, "writer", "revised" if revising else "drafted", f"보고서 {len(report)}자")
    # 새 초안이므로 이전 평가 결과는 지운다 → supervisor가 다시 evaluator로 보낸다
    return {"report": report, "eval_result": None, "node_status": {"writer": "ok"}, "last_error": None}
