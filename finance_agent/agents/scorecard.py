"""투자 평가 스코어카드.

LLM은 항목별 1~5점·근거 ID·한 줄 사유만 고르고, 가중합(100점 환산)은 코드가 계산한다.
근거가 없는 항목은 점수를 매기지 않고 결측(None)으로 남기며, 결측 항목은 분모에서 빠진다.
점수표는 writer가 아니라 코드가 보고서에 직접 삽입한다 (숫자 변형 방지).
"""

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from .. import config, llm
from ..prompts import SCORECARD_PROMPT
from ..state import ScoreItem, ScoreKey, Scorecard


class ScoreDraftItem(BaseModel):
    key: ScoreKey
    score: int | None = Field(default=None, description="1~5. 근거가 부족하면 null")
    rationale: str = Field(description="점수 사유 한 문장 (근거 수치·사실 포함)")
    evidence_ids: list[str] = Field(default_factory=list)


class ScoreDraft(BaseModel):
    items: list[ScoreDraftItem]


def compute(draft: ScoreDraft, valid_ids: set[str]) -> Scorecard:
    by_key = {i.key: i for i in draft.items}
    items, weighted, covered = [], 0.0, 0
    for key, spec in config.SCORECARD.items():
        d = by_key.get(key)
        ids = [e for e in (d.evidence_ids if d else []) if e in valid_ids]
        score = d.score if d and d.score is not None and 1 <= d.score <= 5 and ids else None
        if score is not None:
            weighted += spec["weight"] * score
            covered += spec["weight"]
        items.append(ScoreItem(key=key, label=spec["label"], weight=spec["weight"], score=score,
                               rationale=(d.rationale if d else "") if score is not None
                               else (d.rationale if d and d.rationale else "근거 부족으로 결측"),
                               evidence_ids=ids))
    total = round(weighted / (5 * covered) * 100, 1) if covered else None
    return Scorecard(items=items, total=total, covered_weight=covered)


def build(perspectives: dict, evidence_catalog: str, valid_ids: set[str]) -> Scorecard:
    findings = "\n\n".join(
        f"[{key}] {config.PERSPECTIVES[key]['title']} (근거 ID: {', '.join(p.evidence_ids) or '-'})\n{p.findings}"
        for key, p in perspectives.items() if p and p.findings)
    draft: ScoreDraft = llm.structured(
        ScoreDraft,
        [SystemMessage(SCORECARD_PROMPT.format(
            items="\n".join(f"- {k}: {v['label']} (가중치 {v['weight']})" for k, v in config.SCORECARD.items()),
            evidence_catalog=evidence_catalog)),
         HumanMessage(findings or "(정리된 관점 결과 없음)")],
        run_name="scorecard",
    )
    return compute(draft, valid_ids)


def render(sc: Scorecard) -> str:
    rows = ["| 평가 항목 | 가중치 | 점수 (1~5) | 근거 | 사유 |", "|---|---:|:---:|---|---|"]
    for it in sc.items:
        score = "결측" if it.score is None else str(it.score)
        cites = " ".join(f"[{e}]" for e in it.evidence_ids) or "-"
        rows.append(f"| {it.label} | {it.weight} | {score} | {cites} | {it.rationale.replace('|', '/')} |")
    total = "산출 불가 (모든 항목 결측)" if sc.total is None else f"**{sc.total:.1f} / 100**"
    rows.append(f"| **종합 (가중 환산)** | {sc.covered_weight} | {total} | | 결측 항목은 분모에서 제외 |")
    note = ("> 점수는 수집된 근거만으로 매긴 평가 보조 지표이며 투자 결정이 아닙니다. "
            f"평가 반영 가중치 {sc.covered_weight}/100.")
    return "### 투자 평가 스코어카드\n" + "\n".join(rows) + "\n\n" + note
