"""투자 평가 항목과 스코어카드 계산.

평가 항목의 정의는 여기 한 곳에만 둔다. 프롬프트의 `{criteria}` 자리와 score_investment 도구가 함께 쓴다.
점수는 LLM이 항목별로 1~5점을 매기고, 가중 합산과 투자 의견은 Python이 계산한다.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Criterion:
    key: str
    name: str
    weight: int  # 가중치(%). 합계 100
    evidence: str  # 이 항목을 평가하는 데 필요한 근거


CRITERIA = (
    Criterion("team", "팀·창업자", 20,
              "창업자와 핵심 인력의 경력·전문성, 로보틱스·AI 연구/사업 경험, 조직 규모"),
    Criterion("technology", "기술력·차별성", 25,
              "핵심 기술(로봇 하드웨어, 제어, Physical AI 모델, 데이터), 특허·논문, 기술 성숙도, 경쟁사 대비 차별점"),
    Criterion("market", "시장성", 20,
              "목표 시장의 규모와 성장률, 수요 동인, 정책·규제 환경"),
    Criterion("traction", "사업화·트랙션", 15,
              "제품 출시·양산·실증 현황, 고객과 파트너, 매출·수주"),
    Criterion("competition", "경쟁 우위", 10,
              "주요 경쟁사와 비교한 포지션, 진입장벽"),
    Criterion("finance", "재무·투자 유치", 10,
              "누적 투자 유치 규모와 투자자, 기업가치, 매출·손익, 현금 여력"),
)
BY_KEY = {c.key: c for c in CRITERIA}

MAX_SCORE = 5
MIN_EVALUATED_WEIGHT = 70  # 평가한 항목의 가중치 합이 이보다 작으면 의견을 유보한다
VERDICTS = ((75, "투자 긍정 검토"), (60, "조건부 검토"), (0, "보류"))


def render() -> str:
    """프롬프트에 넣을 평가 항목 목록."""
    return "\n".join(f"- {c.name} (key: {c.key}, 가중치 {c.weight}%): {c.evidence}" for c in CRITERIA)


def total(scores: dict[str, int | None]) -> tuple[float | None, int, str]:
    """(100점 환산 종합 점수, 평가한 항목의 가중치 합, 투자 의견).

    근거가 없어 점수를 매기지 못한 항목(None)은 빼고 나머지 가중치로 다시 환산한다.
    """
    evaluated = [(BY_KEY[k], s) for k, s in scores.items() if s is not None]
    weight = sum(c.weight for c, _ in evaluated)
    if weight == 0:
        return None, 0, "판단 유보 (근거 부족)"
    score = sum(c.weight * s for c, s in evaluated) / (weight * MAX_SCORE) * 100
    if weight < MIN_EVALUATED_WEIGHT:
        return score, weight, "판단 유보 (근거 부족)"
    return score, weight, next(label for threshold, label in VERDICTS if score >= threshold)
