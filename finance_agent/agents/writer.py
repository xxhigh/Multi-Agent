"""Writer 노드: 관점별 결과 + 근거로 보고서 작성.

숫자가 바뀌면 안 되는 부분(스코어카드 표, 감사보고서 재무 원본 표, REFERENCE)은 모델이 아니라 코드가 삽입한다.
"""

import re
from datetime import date

from langchain_core.messages import HumanMessage, SystemMessage

from .. import config, llm
from ..observability import doc_sources, load_evidence, log_decision
from ..prompts import REVISION_PROMPT, WRITER_PROMPT
from ..state import EvidenceRef, FinanceState, PerspectiveResult, Scorecard
from ..tools import FINANCIAL_TABLE_MARK
from . import scorecard as scorecard_mod

DISCLAIMER = "본 보고서는 공개 자료 기반의 평가 참고 자료이며 투자 권유가 아닙니다. 최종 투자 판단은 투자위원회의 몫입니다."
INVESTMENT_HEADING = "## INVESTMENT ASSESSMENT & RISKS"
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
    return f"### {spec['title']}{flag}\n근거 ID: {ids}\n{body}{gaps}"


def build_reference(report: str, evidence: list[EvidenceRef]) -> str:
    """본문에 실제로 인용된 근거만 REFERENCE로 만든다 (인용 ↔ 출처 1:1 연결)."""
    by_id = {e.id: e for e in evidence}
    cited = list(dict.fromkeys(f"E{n}" for n in CITATION.findall(report)))
    lines = ["## REFERENCE"]
    for eid in cited:
        e = by_id.get(eid)
        if e is None:
            continue
        sources = doc_sources(load_evidence(e.path)) if e.type in ("company_doc", "industry_doc") else []
        if sources:
            lines.append(f"- [{e.id}] {e.provider} (문서 풀 검색)")
            lines += [f"    - {desc}" + (f" — {url}" if url else "") for desc, url in sources]
            continue
        url = f" — {e.url}" if e.url else ""
        lines.append(f"- [{e.id}] {e.provider} · {e.title}{url}")
    if len(lines) == 1:
        lines.append("- (인용된 근거 없음)")
    return "\n".join(lines)


def financial_block(evidence: list[EvidenceRef]) -> str:
    """가장 최근 감사보고서 재무 근거의 원본 표만 잘라 붙인다 (숫자 변형 없음)."""
    fin = [e for e in evidence if e.type == "financials"]
    if not fin:
        return "### 재무 요약 (DART 감사보고서)\n수집된 감사보고서 재무표 없음 — 재무 판단은 한계로 둔다."
    e = fin[-1]
    rows = [ln for ln in load_evidence(e.path).splitlines() if ln.startswith("|")]
    return f"### 재무 요약 (DART 감사보고서 원본 표) [{e.id}]\n" + "\n".join(rows)


def finalize_markdown(body: str, evidence: list[EvidenceRef], scorecard: Scorecard | None = None) -> str:
    """모델 출력 정리: 내부 표식·모델이 쓴 REFERENCE·면책 문구 제거 → 표·REFERENCE·면책 문구를 코드로 붙임."""
    lines = [ln for ln in body.splitlines() if FINANCIAL_TABLE_MARK not in ln]
    text = "\n".join(lines)
    text = re.split(r"^##\s*REFERENCE.*$", text, flags=re.M)[0]
    text = text.replace(DISCLAIMER, "").rstrip()
    tables = (scorecard_mod.render(scorecard) + "\n\n" if scorecard else "") + financial_block(evidence)
    if INVESTMENT_HEADING in text:
        text = text.replace(INVESTMENT_HEADING, f"{INVESTMENT_HEADING}\n\n{tables}\n", 1)
    else:
        text = f"{text}\n\n{INVESTMENT_HEADING}\n\n{tables}"
    return f"{text}\n\n{build_reference(text, evidence)}\n\n{DISCLAIMER}\n"


def writer_node(state: FinanceState) -> dict:
    trace_id = state["trace_id"]
    evidence = state.get("evidence") or []
    perspectives = state.get("perspectives") or {}

    catalog = [f"[{e.id}] ({e.provider}) {e.title}\n{load_evidence(e.path, _EXCERPT)}" for e in evidence]

    sc = state.get("scorecard")
    out: dict = {}
    if sc is None and evidence:
        # investment_analyst가 스코어카드를 못 만든 경우의 대체 경로 (한 번만 시도)
        try:
            sc = scorecard_mod.build(perspectives, "\n".join(f"- {e.id} [{e.provider}] {e.title}" for e in evidence),
                                     {e.id for e in evidence})
            out["scorecard"] = sc
        except Exception as e:  # noqa: BLE001
            log_decision(trace_id, "writer", "scorecard_error", f"{type(e).__name__}: {e}")

    user = "\n\n".join([
        f"사용자 요청: {state['question']}",
        "## 관점별 분석 결과\n" + "\n\n".join(_perspective_block(k, perspectives.get(k)) for k in config.PERSPECTIVE_KEYS),
        "## 스코어카드 (시스템이 표로 삽입함, 본문에서는 해석만)\n" + (scorecard_mod.render(sc) if sc else "(없음 — 점수 언급 금지)"),
        "## 근거 목록 (발췌)\n" + ("\n\n".join(catalog) or "(없음)"),
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

    report = finalize_markdown(body, evidence, sc)
    log_decision(trace_id, "writer", "revised" if revising else "drafted", f"보고서 {len(report)}자")
    # 새 초안이므로 이전 평가 결과는 지운다 → supervisor가 다시 evaluator로 보낸다
    return {**out, "report": report, "eval_result": None, "node_status": {"writer": "ok"}, "last_error": None}
