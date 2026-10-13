"""LLM 호출 창구.

모든 에이전트가 이 모듈을 통해서만 모델을 부른다.
→ 모델 교체가 한 곳에서 끝나고, 테스트에서는 이 함수들만 가짜로 바꿔 끼우면 API 없이 그래프 전체를 돌릴 수 있다.
"""

import os

from langchain_openai import ChatOpenAI


def chat_model() -> ChatOpenAI:
    return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-5-mini"))


def judge_model() -> ChatOpenAI:
    """품질 평가(LLM Judge)용 모델. 생성 모델과 따로 지정할 수 있다."""
    return ChatOpenAI(model=os.getenv("OPENAI_JUDGE_MODEL", os.getenv("OPENAI_MODEL", "gpt-5-mini")))


def structured(schema, messages, *, run_name: str, judge: bool = False):
    """Pydantic 스키마로 구조화된 응답을 받는다 (라우팅·충분성·평가 판정용)."""
    model = judge_model() if judge else chat_model()
    return model.with_structured_output(schema).invoke(messages, config={"run_name": run_name})


def text(messages, *, run_name: str) -> str:
    return chat_model().invoke(messages, config={"run_name": run_name}).content
