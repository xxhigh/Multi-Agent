# finance_agent

국내 상장사 재무 분석 멀티 에이전트입니다.
회사 이름을 입력하면 **Supervisor · Researcher · Web Searcher · Analyst · Writer** 에이전트가 협업해 DART 공시와 사업보고서, 웹 검색 결과를 바탕으로 재무 분석 리포트를 작성합니다.

## 에이전트 설계

### 전체 구조

LangGraph의 **Supervisor 패턴**으로 구성했습니다. Supervisor가 매 턴 증거가 충분한지 판단한 뒤 다음 작업을 정하고(서로 독립인 작업은 동시에), 각 에이전트는 작업을 끝내면 다시 Supervisor에게 돌아옵니다.

```
                              ┌─Send─▶ researcher ───┐
 START → init → supervisor ───┼─Send─▶ web_searcher ─┼─▶ supervisor → … → writer → supervisor → END
                              └─Send─▶ analyst ──────┘
```

| 에이전트 | 역할 | 사용 도구 |
|---|---|---|
| **Supervisor** | 증거 충분성 판단, 다음 작업자 선택 및 지시 작성 (structured output으로 라우팅) | - |
| **Researcher** | 기업 개황, 재무제표, 주가, 최근 공시, 사업보고서 본문 수집, 문서 풀에서 산업·정책 근거 검색 | `search_company`, `get_company_overview`, `get_financial_statements`, `get_price_summary`, `get_recent_disclosures`, `search_business_report`, `search_documents` |
| **Web Searcher** | 최근 뉴스, 업계·경쟁사 동향, 증권가 시각 등 DART에 없는 외부 정보를 웹 검색으로 수집 (출처 URL 포함) | `web_search` (Tavily) |
| **Analyst** | 성장성·수익성·안정성·현금흐름·밸류에이션 지표 계산 및 해석 | `calculate_financial_ratios`, `search_business_report`, `calculator` |
| **Writer** | 수집·분석 결과만으로 한국어 Markdown 리포트 작성 | - |

### 라우팅 규칙

정해진 순서 없이 Supervisor가 매 턴 `Route(sufficient, missing, reason, assignments)` Pydantic 스키마로 결정합니다.

- **증거 충분성 판단**: 분석·비교 요청은 성장성 · 수익성 · 안정성 · 현금흐름 · 밸류에이션 · 주가 동향 · 사업 구조/실적 변동 원인 · 리스크/최근 공시 · 최근 뉴스/업계 동향 관점마다 근거가 있어야 충분하다고 봅니다. 단순 사실 질문은 그 사실의 근거만 있으면 됩니다.
- **관점별 재조사 루프**: 근거가 부족한 관점(`missing`)이 있으면 재무제표·공시·사업보고서는 researcher, 외부 정보는 web_searcher, 계산·해석은 analyst에게 그 관점만 다시 지시합니다.
- **동적 fan-out**: 서로의 결과가 필요 없는 작업은 `Send`로 최대 3개까지 동시에 내보냅니다 (예: researcher + web_searcher, 비교 기업별 researcher).
- **보고서 작성 조건**: writer는 `sufficient`가 true일 때만, 단독으로 호출됩니다. 부족한데 writer만 고르면 코드에서 researcher로 되돌립니다.
- **결정 사유**: 모든 결정은 `reason`과 함께 트레이스에 기록됩니다.
- writer가 리포트를 쓰면 LLM 호출 없이 바로 종료합니다.
- 해외 기업이나 재무와 무관한 요청: writer가 지원 범위를 안내

### State 설계

스키마는 `agents/state.py`에 있습니다.

| 구분 | 필드 | 내용 |
|---|---|---|
| 페이로드 | `messages` | 사용자 질문과 최종 리포트 (후속 질문의 맥락). 최근 7개만 유지 |
| 페이로드 | `findings` | 이번 질문에서 worker가 낸 결과. 질문이 시작될 때 비움 |
| 페이로드 | `report` | 이번 질문의 최종 리포트 |
| 제어 메타 | `run_id` | 질문 한 건의 실행 ID. 트레이스 파일과 잇는 키 |
| 제어 메타 | `status` | `running` / `done` / `failed` |
| 제어 메타 | `step`, `rounds` | Supervisor 결정 횟수, 조사 작업 수 (상한 관리) |
| 제어 메타 | `decision` | 마지막 결정 (충분성, 부족한 관점, 사유, 내보낸 작업) |
| 제어 메타 | `errors` | 이번 질문에서 실패한 작업 (재시도 판단용) |

| 항목 | 설계 |
|---|---|
| 제어 vs 페이로드 분리 | 위 표처럼 필드를 나눴습니다. Supervisor의 조정 로직(종료·상한·재시도)은 제어 메타만 읽고, 페이로드는 LLM에 보여 줄 때만 씁니다. worker는 전체 State가 아니라 `WorkerInput`(대화 + 지시)만 받습니다. |
| 관측성 위치 | State에는 마지막 결정 하나만 두고, 결정 이력·사유·작업 결과 본문은 외부 트레이스 `outputs/traces/<run_id>.jsonl`에 남깁니다. |
| 지속성 비용 | `findings`·`errors`는 질문마다 초기화하고, `messages`는 길이를 제한합니다. worker 내부의 도구 호출 기록(사업보고서 본문 등)은 체크포인트에 저장하지 않습니다. |
| 상관 | 트레이스의 모든 줄에 `run_id`와 `step`이 있고, 시작 이벤트에 `thread_id`, 종료 후 리포트 파일 경로가 기록됩니다. |
| 재개/복구 | `status`, `step`, `rounds`, `decision.tasks`(하던 작업), `errors`(시도 횟수)가 State에 있습니다. worker 오류는 State에 기록해 1회 재시도하고, 실행이 중단되면 마지막 체크포인트에서 이어서 실행합니다. |
| 동시 처리 | 병렬 worker가 쓰는 `findings`, `errors`, `messages`에는 reducer가 있습니다. 나머지 필드는 한 번에 한 노드(init, supervisor, 단독 실행되는 writer)만 씁니다. |
| 종료 보장 | 결정 10회(`MAX_STEPS`), 조사 작업 6건(`MAX_RESEARCH_ROUNDS`), 작업당 시도 2회(`MAX_ATTEMPTS`), worker 내부 도구 호출 8회. 상한에 닿으면 writer로 넘기고 근거를 못 구한 관점은 "확인되지 않음"으로 밝힙니다. |

### 설계 포인트

1. **에이전트마다 ReAct 서브그래프**
   Researcher, Web Searcher, Analyst는 각각 `LLM ↔ ToolNode` 루프를 도는 서브그래프입니다. 메인 대화에는 서브그래프의 최종 답변만 남겨서, 도구 호출 기록 때문에 컨텍스트가 커지지 않게 했습니다.

2. **숫자는 LLM이 아니라 Python이 계산**
   재무 비율(ROE, PER, FCF 등)은 `calculate_financial_ratios`가 DART 원천 데이터로 직접 계산합니다. 추가 계산도 AST 기반 안전한 `calculator` 도구를 거치게 해서 LLM의 암산 오류를 막았습니다.
   PER은 분기 보고서가 있으면 **TTM(최근 12개월) 지배주주 순이익** 기준으로 계산합니다.

3. **환각 방지 프롬프트**
   모든 에이전트에 "도구 결과에 있는 수치만 사용하고, 없는 값은 만들지 않는다"는 규칙을 넣었습니다. Writer는 대화에 있는 수치만 인용하고 출처(DART / Yahoo Finance / 웹 검색 링크)를 표기합니다.

4. **사업보고서 RAG (하이브리드 검색)**
   - DART에서 최신 사업보고서 원문(XML)을 내려받아 목차 기준으로 **서술형 섹션만** 잘라냅니다 (사업의 내용, 경영진단 및 분석의견, 투자자 보호 사항, 영업부문·우발채무 주석). 재무제표 숫자는 API로 정확하게 조회하므로 제외했습니다.
   - 표는 행 단위 텍스트로 바꿔 구조를 유지하고, 1,000자 chunk(150자 overlap)로 나눠 OpenAI 임베딩 후 Chroma에 저장합니다. 보고서(접수번호)별로 한 번만 인덱싱합니다.
   - 검색은 벡터 유사도로 후보 30개를 뽑은 뒤 **한국어 bigram 키워드 점수**를 더해 다시 정렬합니다. 조사나 띄어쓰기가 달라도 키워드가 잘 맞습니다.

5. **문서 풀 RAG**
   - `data/docs`의 PDF(정부 AI반도체 산업 전략, KISDI 시장·기술 동향 보고서, Deloitte 전망)로 미리 만든 FAISS 인덱스(`data/vectorstores/BAAI--bge-m3`)를 `search_documents` 도구로 검색합니다.
   - 질의는 인덱스와 같은 `BAAI/bge-m3` 모델로 로컬에서 임베딩합니다. 처음 검색할 때 모델(약 2.3GB)을 내려받습니다.
   - 사업보고서 RAG와 같은 방식으로 벡터 유사도 후보에 bigram 키워드 점수를 더해 재정렬하고, 결과마다 문서 제목·발행기관·날짜·쪽수·URL을 붙입니다.

6. **안정성**
   - 에이전트 한 번의 작업에서 도구 호출이 8번을 넘으면 도구 없이 최종 답변을 강제하고, Supervisor 결정 수와 조사 작업 수에도 상한을 둬서 무한 루프를 막습니다.
   - DART·웹 검색 오류는 예외 대신 LLM이 읽을 수 있는 문자열로 돌려줘서, 에이전트가 스스로 다른 방법을 시도합니다.
   - 약칭(현대차, 삼전, 엔솔 등)을 정식 회사명으로 바꾸고, 회사명이 애매하면 후보 목록을 돌려줍니다.
   - 연결재무제표가 없으면 별도재무제표로 대체하고, 금융사는 ROE·PBR·배당성향 중심으로 해석합니다.

7. **실시간 진행 상황 표시**
   `graph.stream(subgraphs=True)` 업데이트를 받아 Supervisor의 결정과 사유, 에이전트별 도구 호출, 작업 완료·실패를 터미널에 바로 출력합니다.
   `InMemorySaver` 체크포인터를 써서 대화형 모드에서는 같은 `thread_id`로 후속 질문을 이어가거나, 오류로 중단된 실행을 `resume`으로 재개할 수 있습니다.

---

## 기술 스택

| 구분 | 기술 |
|---|---|
| **Agent** | LangGraph (Supervisor + ReAct 서브그래프), LangChain Core |
| **LLM** | OpenAI `gpt-5-mini` (`OPENAI_MODEL`로 변경 가능), `text-embedding-3-small` |
| **RAG** | 사업보고서: ChromaDB + OpenAI 임베딩 / 문서 풀: FAISS + `BAAI/bge-m3` (로컬) / 벡터 + bigram 키워드 하이브리드 검색 |
| **데이터** | OpenDART API (재무제표·기업개황·공시·사업보고서 원문), yfinance (주가·시가총액), Tavily (웹 검색) |
| **실행** | Python CLI |

---

## 프로젝트 구조

```
.
├── data/                  # 문서 풀
│   ├── docs/              # 원본 PDF (산업 전략·정책, 전망 보고서)
│   ├── vectorstores/      # 문서 풀 FAISS 인덱스 (BAAI--bge-m3)
│   └── chroma/, corp_codes.json   # DART 사업보고서 인덱스·기업 코드 캐시 (실행 시 생성)
├── agents/                # Agent 모듈
│   ├── graph.py           # Supervisor 멀티 에이전트 그래프
│   ├── state.py           # State 스키마 (제어 메타 / 페이로드)
│   ├── trace.py           # 실행 트레이스 (JSONL)
│   ├── tools.py           # 데이터 수집 · 재무 비율 계산 도구
│   ├── dart.py            # OpenDART API 클라이언트
│   ├── rag.py             # 사업보고서 파싱 · 인덱싱 · 하이브리드 검색
│   └── docs.py            # 문서 풀(FAISS) 검색
├── prompts/               # 프롬프트 템플릿
│   ├── supervisor.md
│   ├── researcher.md
│   ├── web_searcher.md
│   ├── analyst.md
│   └── writer.md
├── outputs/               # 실행 결과 저장 (report_YYYYMMDD_HHMMSS.md, traces/<run_id>.jsonl)
├── app.py                 # 실행 스크립트 (CLI)
├── requirements.txt
└── README.md
```

## 실행 방법

### 1. 환경 변수

루트에 `.env` 파일을 만듭니다.

```env
OPENAI_API_KEY=sk-...
OPENDART_API_KEY=...      # https://opendart.fss.or.kr 에서 발급
TAVILY_API_KEY=tvly-...   # https://tavily.com 에서 발급 (웹 검색용)
OPENAI_MODEL=gpt-5-mini   # 선택
```

### 2. 설치

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 실행

```bash
python app.py                         # 대화형 모드
python app.py "셀트리온 재무 분석해줘"   # 단일 질문
```

진행 상황이 터미널에 출력되고, 완성된 리포트는 `outputs/report_YYYYMMDD_HHMMSS.md`로, 결정·사유·작업 결과는 `outputs/traces/<run_id>.jsonl`로 저장됩니다.

---

> 본 프로젝트의 리포트는 정보 제공 목적이며 투자 권유가 아닙니다.
