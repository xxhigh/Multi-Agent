# Subject
본 프로젝트는 국내 상장사 1곳을 **사업·기술 경쟁력, 성장성·시장 평가, 주주·지배구조·공시, 재무 건전성·산업 특성** 4개 관점에서 분석하는 보고서를
**Supervisor 패턴** 기반 멀티 에이전트로 설계·개발한 프로젝트임.
DART 공시·재무제표·사업보고서(RAG)와 Yahoo Finance 시세를 근거로 쓰며, 보고서는 특정 판단을 권하지 않는 **중립적 다관점 분석**을 목표로 함.

## Overview
- **Objective** : 하나의 기업을 4개 관점에서 근거 기반으로 분석하고, 품질 평가를 통과한 보고서만 내보낸다
- **Pattern** : Supervisor — 보고서 작성 전에 "근거가 충분한가"를 중앙에서 판단하고, 부족한 관점만 골라 재조사시키는 흐름이 필요했다.
  사전 분해(Orchestrator)보다 수집 결과에 따라 다음 행동이 바뀌는 구조가 이 문제에 맞다고 판단했다.
- **동적 처리** : 실행 순서가 코드에 없다. supervisor가 매 턴 State(관점별 충분성·평가 결과·상한)를 보고 `next`를 정하고,
  `add_conditional_edges`가 그 값으로 분기한다. 같은 질문이라도 수집 결과에 따라 경로가 달라진다
  (예: `researcher → analyst → researcher(공시 재작업) → writer → evaluator → writer(수정) → evaluator → FINISH`).

## Analysis Perspectives (과제 4관점 매핑)

| 과제 관점 | 보고서 절 | 작성 담당 | 충분성 판정에 필요한 근거 |
|---|---|---|---|
| 기술 성숙도 | 1. 사업·기술 경쟁력 | researcher | 사업보고서 RAG |
| 시장성 | 2. 성장성·시장 평가 | analyst | 주가 요약 + 재무 비율 |
| 이해관계자 | 3. 주주·지배구조·공시 | researcher | 최근 공시 + 기업개황 |
| 도메인 적용 | 4. 재무 건전성·산업 특성 | analyst | 재무 비율 + 재무제표 (업종 특성 반영, 금융사 예외 처리) |

## Features
- **PDF/공시 자료 기반 정보 추출** : DART 사업보고서 원문(XML)에서 서술형 섹션만 잘라 RAG 인덱싱, 표는 행 단위 텍스트로 보존
- **하이브리드 검색** : 벡터 유사도 후보 30개 + 한국어 bigram 키워드 점수로 재정렬
- **숫자는 Python이 계산** : 재무 비율·TTM PER은 도구가 직접 계산, 추가 계산은 AST 기반 안전 계산기만 허용
- **근거 충분성 판정 (Supervisor)** : 규칙 검사(필수 근거 종류·내용·근거 ID 연결)를 먼저 하고, 통과한 관점만 LLM이 충분성 판정
- **재작업 요청** : 부족한 관점만 골라 그 근거를 모으는 담당(researcher/analyst)에게 사유와 함께 되돌림 (관점별 최대 3회)
- **확증 편향 방지 전략** : 관점별 서로 다른 근거 2개 이상 인용 규칙, 보고서 전체 출처 3종 이상 규칙, 악화 지표 동시 서술 지시, 우열·추천 표현 금지
- **보고서 품질 평가** : 규칙 검사 → LLM Judge의 Hybrid 평가(Groundedness·중립성·편향 통제·관점 커버리지). 미달 시 문장 문제는 writer 수정, 근거 부족은 수집 담당 재조사로 Loop
- **인용 ↔ 출처 1:1 연결** : 본문 `[E#]` 인용을 코드가 읽어 REFERENCE 절을 자동 생성 (모델이 출처를 지어낼 수 없음)

## Tech Stack
- Framework : LangGraph (Supervisor + ReAct 서브그래프), LangChain Core
- LLM/Generator : OpenAI `gpt-5-mini` (`OPENAI_MODEL`로 변경)
- LLM/Judge : OpenAI `gpt-5-mini` (`OPENAI_JUDGE_MODEL`로 생성 모델과 분리 가능)
- Retrieval : ChromaDB (Persistent, cosine) + bigram 키워드 재정렬 — Hit Rate@K, MRR : 미측정
- Embedding : OpenAI `text-embedding-3-small`
- Data : OpenDART API (재무제표·기업개황·공시·사업보고서 원문), yfinance (주가·시가총액)
- Tracing : LangSmith (`metadata.trace_id`로 로컬 결정 로그와 연결)
- Report Export : markdown → PDF (PyMuPDF, 한글 글꼴 내장)
- Backend / Frontend : FastAPI + SSE / Next.js (선택 사항)

## Agents
- **Supervisor** : 대상 기업·범위 접수, 관점별 근거 충분성 판정, 허용 후보 안에서 다음 담당 선택, 재작업·수정 요청, 종료 결정. 하위 에이전트끼리는 엣지가 없고 모든 통신은 supervisor를 거친다
- **Researcher** : 기업개황·재무제표·주가·공시·사업보고서 RAG 수집 → 담당 관점(사업·기술, 주주·공시) 정리
- **Analyst** : 재무 비율 계산·해석 → 담당 관점(성장성·시장, 재무 건전성) 정리
- **Writer** : 관점별 결과와 근거만으로 보고서 작성 (SUMMARY → 1~4 관점 → 원본 표 → 한계 → REFERENCE)
- **Evaluator** : 보고서 품질 평가 노드 (규칙 검사 + LLM Judge), 실패 원인에 따라 되돌릴 담당 지정

### Supervisor 필수 항목 구현 위치

| 과제 필수 항목 | 구현 |
|---|---|
| 하위 에이전트는 Supervisor하고만 통신 | `graph.py` — worker → supervisor 엣지만 존재, worker 간 엣지 없음 |
| State 기반 `add_conditional_edges` 분기, 순서 하드코딩 금지 | `supervisor.py`가 `next` 기록 → `graph.route_after_supervisor()`(순수 함수)가 분기 |
| 근거 충분성 평가 후에만 보고서 작성 (스텝 고정 금지) | `supervisor.py` 4단계 — 4관점 모두 충분하거나 재작업 한도 소진 시에만 `writer`가 허용 후보가 됨 |
| 근거 부족 시 해당 하위 에이전트 재작업 | `responsible_workers()` — 없는 근거의 수집 담당 / 내용이 부족하면 작성 담당에게 사유와 함께 재지시 (`is_rework` 로그) |

판단과 분기를 나눴다. LLM은 충분성 판정과 '허용된 후보 중' 다음 담당만 고르고, 허용 후보 계산·상한 검사는 코드가 한다.
LLM이 후보 밖을 고르면 코드가 기본 경로로 교체하고 그 사실을 결정 로그(`llm_override`)에 남긴다.

## State Schema
코드: [`finance_agent/state.py`](finance_agent/state.py)

```python
class FinanceState(MessagesState):
    # 작업 페이로드
    question, company: str
    perspectives: Annotated[dict[str, PerspectiveResult], merge_dict]   # 관점별 결과·근거 ID·충분성
    evidence: Annotated[list[EvidenceRef], operator.add]                # 근거 참조 (원문은 파일)
    report: str | None
    eval_result: EvalVerdict | None
    # 제어 메타데이터
    trace_id: str; next: str; instruction: str; target_perspectives: list[str]
    step_count: int; max_steps: int
    attempts: Annotated[dict[str, int], merge_dict]; revision_count: int
    node_status: Annotated[dict[str, str], merge_dict]; last_error: str | None; status: str
```

- **제어 vs 페이로드 분리** : 위 두 블록으로 분리. supervisor 라우팅은 제어 메타와 관점별 `sufficient` 플래그만 보고 결정한다
- **관측성 위치** : 결정 로그(노드·결정·사유·대상 관점·재작업 여부)는 State가 아니라 `outputs/runs/{trace_id}/decisions.jsonl`에 적재
- **지속성 비용** : 도구 원문(재무제표 표, 사업보고서 발췌 등)은 `evidence/E*.md` 파일로 저장하고 State에는 `EvidenceRef`만 둔다. `messages`는 사용자 질문과 최종 보고서만 담고, 하위 에이전트에게는 필요한 정보만 골라 새 입력으로 준다
- **상관** : `trace_id` 하나로 State ↔ decisions.jsonl ↔ evidence 파일 ↔ LangSmith run(`metadata.trace_id`, tag `trace:{id}`)을 연결
- **재개/복구** : `InMemorySaver` checkpointer + `node_status`·`last_error`·`attempts`. 하위 에이전트 예외는 그래프를 멈추지 않고 `error`로 기록되며 supervisor가 재시도 여부를 판단
- **동시 처리** : 누적 필드는 reducer로 병합 — `evidence`는 이어붙이기, `perspectives`·`attempts`·`node_status`는 dict 병합 (같은 관점을 여러 담당이 채우면 내용·근거 ID를 합침)
- **종료 보장** : `config.py` 상수로 코드에서 강제 — supervisor 판단 16회, 관점별 작업 3회(재작업 2회), 품질 미달 수정 2회, ReAct 도구 호출 8회, LangGraph `recursion_limit` 80. 상한 도달 시 `FINISH_WARNING`으로 경고를 붙여 종료

## Architecture
![architecture](docs/images/architecture.png)

## Directory Structure
```
├── finance_agent/
│   ├── graph.py           # Supervisor 그래프 연결 (add_conditional_edges)
│   ├── supervisor.py      # 접수·충분성 판정·라우팅·재작업·종료 결정
│   ├── state.py           # State Schema
│   ├── config.py          # 상한·관점 정의 (코드로 강제하는 정책)
│   ├── observability.py   # trace_id, 결정 로그, 근거 파일
│   ├── runner.py          # 초기 State·실행 config·산출물 저장 (CLI·API 공용)
│   ├── llm.py             # 모델 호출 창구
│   ├── agents/            # researcher·analyst(workers.py), writer, evaluator
│   ├── prompts/           # 에이전트별 프롬프트 템플릿
│   └── tools/             # OpenDART 클라이언트, 사업보고서 RAG, 재무 도구
├── tests/                 # 가짜 LLM으로 라우팅·재작업·평가 루프·종료 검증
├── outputs/               # 실행 결과 (reports/, runs/{trace_id}/)
├── backend/ frontend/     # FastAPI SSE API, Next.js UI (선택)
├── docs/                  # 아키텍처 그림
├── main.py                # CLI 실행 스크립트
└── README.md
```

## Usage
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # OPENAI_API_KEY, OPENDART_API_KEY, LangSmith 키 입력

python main.py "셀트리온 재무 분석해줘"
```

실행이 끝나면 다음이 생성된다.

| 파일 | 내용 |
|---|---|
| `outputs/reports/report_{trace_id}.md` | 최종 보고서 (마크다운) |
| `outputs/reports/report_{trace_id}.pdf` | 최종 보고서 (제출용 PDF, A4·쪽번호) |
| `outputs/runs/{trace_id}/decisions.jsonl` | supervisor 라우팅·재작업·평가 결정과 사유 |
| `outputs/runs/{trace_id}/run-summary.json` | 라우팅 순서, 재작업 횟수, 수정 횟수, 최종 상태 |
| `outputs/runs/{trace_id}/evidence/E*.md` | 근거 원문 |

테스트 (API 키 불필요):
```bash
pytest -q
```

웹 UI (선택): `uvicorn backend.app:app --port 8000` 후 `cd frontend && npm install && npm run dev`

## Contributors
- 심혁 : Supervisor 재설계(충분성 판정·재작업·종료 가드), State Schema, 품질 평가 노드, 관측성 계층, 테스트, README
- (원본 작성자) : 재무 도구·OpenDART 클라이언트·사업보고서 RAG·웹 UI

> 본 프로젝트의 보고서는 정보 제공 목적이며 투자 권유가 아닙니다.
