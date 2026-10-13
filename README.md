# AI Startup Investment Evaluation Agent

Physical AI / Robotics 스타트업의 투자 가능성을 조사·평가하는 LangGraph Multi-Agent 프로젝트입니다.
기업 정보와 기술 문서에서 근거를 수집하고, 평가 항목별 분석과 출처를 포함한 투자 검토 보고서를 생성합니다.

> 질문 예: `Figure AI 투자 가능성 평가해줘`, `레인보우로보틱스와 두산로보틱스를 투자 관점에서 비교해줘`

---

## 에이전트 설계

### 전체 구조

LangGraph의 **Supervisor 패턴**으로 구성했습니다. Supervisor가 매 턴 증거가 충분한지 판단한 뒤 다음 작업을 정하고(서로 독립인 작업은 동시에), 각 에이전트는 작업을 끝내면 다시 Supervisor에게 돌아옵니다.

```
                              ┌─Send─▶ company_researcher ─┐
 START → init → supervisor ───┼─Send─▶ tech_researcher ────┼─▶ supervisor → evaluator → supervisor
                              └─Send─▶ market_researcher ──┘        → writer → supervisor → END
```

| 에이전트 | 역할 | 사용 도구 |
|---|---|---|
| **Supervisor** | 증거 충분성 판단, 다음 작업 선택 및 지시 작성 (structured output으로 라우팅) | - |
| **Company Researcher** | 기업 정보 수집: 설립·팀·제품·고객·투자 유치 이력·최근 뉴스. 국내 상장사는 기업 개황·재무제표·공시 | `web_search`, `search_company`, `get_company_overview`, `get_financial_statements`, `get_recent_disclosures` |
| **Tech Researcher** | 기술 근거 수집: 핵심 기술, 기술 성숙도, 특허·논문, 기술 동향 속 위치 | `search_documents`, `web_search` |
| **Market Researcher** | 시장·경쟁 근거 수집: 시장 규모·성장 전망·정책, 경쟁사와 포지션 | `search_documents`, `web_search` |
| **Evaluator** | 평가 항목별 점수와 근거, 스코어카드(종합 점수·투자 의견) 산출 | `score_investment`, `calculate_financial_ratios`, `calculator` |
| **Writer** | 조사·평가 결과만으로 정해진 형식(SUMMARY ~ REFERENCE)의 한국어 Markdown 투자 보고서 작성 | - |

### 평가 항목

항목 정의는 `agents/criteria.py` 한 곳에 있고, 프롬프트와 스코어카드 도구가 함께 씁니다.

| 평가 항목 | 가중치 | 보는 근거 |
|---|---:|---|
| 팀·창업자 | 20% | 창업자와 핵심 인력의 경력·전문성, 로보틱스·AI 연구/사업 경험, 조직 규모 |
| 기술력·차별성 | 25% | 핵심 기술(로봇 하드웨어, 제어, Physical AI 모델, 데이터), 특허·논문, 기술 성숙도, 차별점 |
| 시장성 | 20% | 목표 시장의 규모와 성장률, 수요 동인, 정책·규제 환경 |
| 사업화·트랙션 | 15% | 제품 출시·양산·실증 현황, 고객과 파트너, 매출·수주 |
| 경쟁 우위 | 10% | 주요 경쟁사와 비교한 포지션, 진입장벽 |
| 재무·투자 유치 | 10% | 누적 투자 유치 규모와 투자자, 기업가치, 매출·손익, 현금 여력 |

- Evaluator가 항목마다 1~5점과 근거를 정하면 `score_investment` 도구가 가중 합산해 100점 만점의 종합 점수와 투자 의견을 계산합니다: 75점 이상 **투자 긍정 검토**, 60점 이상 **조건부 검토**, 그 미만 **보류**.
- 근거가 없는 항목은 추정하지 않고 **평가 보류**로 두며, 그 항목을 빼고 환산합니다. 평가한 항목의 가중치 합이 70% 미만이면 **판단 유보**입니다.
- 리스크(기술·양산, 규제·안전, 자금, 경쟁)는 점수와 별도로 정리합니다.

### 투자 보고서 형식

보고서는 **SUMMARY**로 시작해 **REFERENCE**로 끝납니다. 장 제목과 순서는 고정입니다.

```
# <기업명> 투자 보고서
## SUMMARY                   # 투자 의견·종합 점수·핵심 근거·최대 리스크 (반 페이지 이내)
## 1. 사업 아이디어            # 핵심 컨셉, 제품과 사업 모델
## 2. 팀 구성                 # 핵심 창업자, 기술 역량
## 3. 기술 분석
## 4. 시장 규모 및 경쟁 구도
## 5. 사업화 및 재무
## 6. 투자 평가               # 스코어카드
## 7. 사업 리스크              # 시장 / 기술 / 규제 / 경쟁 / 기타
## 8. 한계점
## REFERENCE                 # 본문에 실제로 인용한 자료만
```

REFERENCE는 자료 종류별 소제목 아래에 다음 형식으로 적습니다. 본문에서는 `(기관명, 연도)`로 가리키고 URL은 REFERENCE에만 둡니다.

| 종류 | 표기 형식 |
|---|---|
| 기관 보고서 | 발행기관(YYYY). *보고서명*. URL |
| 학술 논문 | 저자(YYYY). 논문제목. *학술지명*, 권(호), 페이지. |
| 웹페이지 | 기관명 또는 작성자(YYYY-MM-DD). *제목*. 사이트명, URL |

형식은 프롬프트로만 맡기지 않고 `agents/report.py`가 검사합니다. 목차, SUMMARY 분량, 리스크 소제목, REFERENCE 항목의 표기 형식을 확인해 어긋나면 위반 사항을 알려 주고 Writer가 한 번 고쳐 쓰게 합니다. 게시일을 알 수 없는 웹페이지는 날짜 자리에 `n.d.`를 씁니다. 단순 사실 질문에는 목차 없이 짧게 답하고 REFERENCE만 붙입니다.

### 라우팅 규칙

정해진 순서 없이 Supervisor가 매 턴 `Route(sufficient, missing, needs_scorecard, reason, assignments)` Pydantic 스키마로 결정합니다.

- **증거 충분성 판단**: 투자 평가 요청은 평가 항목마다, 그리고 리스크에 대해 출처가 달린 공개 근거가 있고 스코어카드까지 있어야 충분합니다. 기준은 "공개 자료로 확인할 수 있는 수준"이며, 비상장사의 감사 재무제표나 계약 원문 같은 비공개 문서는 요구하지 않습니다. 단순 사실 질문은 그 사실의 근거만 있으면 됩니다.
- **항목별 재조사 루프**: 근거가 없는 평가 항목(`missing`)이 있으면 담당 researcher에게 그 항목만 다시 지시합니다.
- **동적 fan-out**: 서로의 결과가 필요 없는 작업은 `Send`로 최대 3개까지 동시에 내보냅니다 (세 researcher 동시 실행, 비교 기업별 조사).
- **스코어카드 보장**: 스코어카드가 필요한 요청(`needs_scorecard`)은 Writer로 가기 전에 Evaluator를 반드시 한 번 거칩니다. 조사 한도에 닿았거나 Supervisor가 바로 Writer를 골라도 코드에서 Evaluator를 먼저 실행합니다.
- **보고서 작성 조건**: Writer는 단독으로 호출되고, 보고서가 나오면 LLM 호출 없이 바로 종료합니다.
- **결정 사유**: 모든 결정은 `reason`과 함께 트레이스에 기록됩니다.
- Physical AI / Robotics 기업의 투자 평가와 무관한 요청: Writer가 지원 범위를 안내

### State 설계

스키마는 `agents/state.py`에 있습니다.

| 구분 | 필드 | 내용 |
|---|---|---|
| 페이로드 | `messages` | 사용자 질문과 최종 보고서 (후속 질문의 맥락). 최근 7개만 유지 |
| 페이로드 | `findings` | 이번 질문에서 worker가 낸 결과. 질문이 시작될 때 비움 |
| 페이로드 | `report` | 이번 질문의 투자 검토 보고서 |
| 제어 메타 | `run_id` | 질문 한 건의 실행 ID. 트레이스 파일과 잇는 키 |
| 제어 메타 | `status` | `running` / `done` / `failed` |
| 제어 메타 | `step`, `rounds` | Supervisor 결정 횟수, 조사 작업 수 (상한 관리) |
| 제어 메타 | `decision` | 마지막 결정 (충분성, 부족한 항목, 사유, 내보낸 작업) |
| 제어 메타 | `errors` | 이번 질문에서 실패한 작업 (재시도 판단용) |

| 항목 | 설계 |
|---|---|
| 제어 vs 페이로드 분리 | 위 표처럼 필드를 나눴습니다. Supervisor의 조정 로직(종료·상한·재시도)은 제어 메타만 읽고, 페이로드는 LLM에 보여 줄 때만 씁니다. worker는 전체 State가 아니라 `WorkerInput`(대화 + 지시)만 받습니다. |
| 관측성 위치 | State에는 마지막 결정 하나만 두고, 결정 이력·사유·작업 결과 본문은 외부 트레이스 `outputs/traces/<run_id>.jsonl`에 남깁니다. |
| 지속성 비용 | `findings`·`errors`는 질문마다 초기화하고, `messages`는 길이를 제한합니다. worker 내부의 도구 호출 기록(검색 결과 본문 등)은 체크포인트에 저장하지 않습니다. |
| 상관 | 트레이스의 모든 줄에 `run_id`와 `step`이 있고, 시작 이벤트에 `thread_id`, 종료 후 보고서 파일 경로가 기록됩니다. |
| 재개/복구 | `status`, `step`, `rounds`, `decision.tasks`(하던 작업), `errors`(시도 횟수)가 State에 있습니다. worker 오류는 State에 기록해 1회 재시도하고, 실행이 중단되면 마지막 체크포인트에서 이어서 실행합니다. |
| 동시 처리 | 병렬 worker가 쓰는 `findings`, `errors`, `messages`에는 reducer가 있습니다. 나머지 필드는 한 번에 한 노드(init, supervisor, 단독 실행되는 writer)만 씁니다. |
| 종료 보장 | 결정 10회(`MAX_STEPS`), 조사 작업 5건(`MAX_RESEARCH_ROUNDS`), 작업당 시도 2회(`MAX_ATTEMPTS`), worker 내부 도구 호출 8회. 조사 한도에 닿으면 지금까지의 근거로 평가하고 보고서를 쓰며, 근거를 못 구한 항목은 "확인되지 않음"으로 밝힙니다. |

### 설계 포인트

1. **에이전트마다 ReAct 서브그래프**
   세 Researcher와 Evaluator는 각각 `LLM ↔ ToolNode` 루프를 도는 서브그래프입니다. 메인 State에는 서브그래프의 최종 답변만 남겨서, 도구 호출 기록 때문에 컨텍스트가 커지지 않게 했습니다.

2. **점수 합산은 LLM이 아니라 Python이 계산**
   종합 점수와 투자 의견은 `score_investment`가, 재무 비율은 `calculate_financial_ratios`가 계산합니다. 스코어카드와 재무제표 표는 요약 과정에서 숫자가 바뀌지 않도록 원본 그대로 보고서까지 전달합니다.

3. **근거 기반 작성**
   모든 에이전트에 "도구 결과에 있는 내용만 쓰고, 없는 값은 만들지 않는다"는 규칙을 넣었습니다. Researcher는 항목마다 출처를 붙이고 회사의 주장과 제3자가 확인한 사실을 구분합니다. Writer는 대화에 있는 내용만 인용하고, 본문에 실제로 쓴 자료만 REFERENCE에 싣습니다.

4. **문서 풀 RAG**
   - `data/docs`의 PDF를 쪽 단위로 읽어 1,000자 chunk(150자 overlap)로 나누고, `BAAI/bge-m3`로 임베딩해 FAISS 인덱스(`data/vectorstores/BAAI--bge-m3`)에 저장합니다. 벡터 DB는 이것 하나만 씁니다.
   - 현재 문서 풀: 미래에셋증권 「피지컬 AI: 휴머노이드로 가는 길」(2025), CSET 「Physical AI: A Primer for Policymakers on AI-Robotics Convergence」(2026), 삼일PwC 「피지컬 AI 생태계 및 새로운 성장 기회 전망」(2026), PwC Nigeria 「Leveraging the Power of AI and Robotics」(2018).
   - 문서별 서지 정보(제목·발행기관·날짜·URL)는 `data/docs/metadata.json`에 적습니다. 검색 결과에 쪽수와 함께 붙어 보고서의 REFERENCE에 쓰입니다.
   - 문서를 바꾸면 `python -m agents.docs`로 인덱스를 다시 만듭니다. 임베딩 모델은 로컬에서 돌리며 처음 쓸 때 약 2.3GB를 내려받습니다.
   - 검색은 벡터 유사도로 후보를 뽑은 뒤 한국어 bigram 키워드 점수를 더해 재정렬합니다.

5. **웹 검색**
   Tavily로 기업 정보, 기술 자료, 시장·경쟁사 동향을 찾습니다. 비상장·해외 스타트업은 웹 검색이 주된 근거 수집 수단입니다.

6. **국내 상장사 공시 (DART)**
   대상이 국내 상장사면 OpenDART에서 기업 개황, 재무제표(연결 우선), 최근 공시를 조회합니다. 비상장·해외 기업은 조회되지 않으며 그 경우 웹 검색 근거만 씁니다.

7. **안정성**
   - 에이전트 한 번의 작업에서 도구 호출이 8번을 넘으면 도구 없이 최종 답변을 강제하고, Supervisor 결정 수와 조사 작업 수에도 상한을 둬서 무한 루프를 막습니다.
   - 도구 오류는 예외 대신 LLM이 읽을 수 있는 문자열로 돌려줘서, 에이전트가 스스로 다른 방법을 시도합니다.
   - worker가 실패하면 한 번 재시도하고, 그래도 실패하면 실패 사실을 남긴 채 다음 단계로 갑니다.

8. **실시간 진행 상황 표시**
   `graph.stream(subgraphs=True)` 업데이트를 받아 Supervisor의 결정과 사유, 에이전트별 도구 호출, 작업 완료·실패를 터미널에 바로 출력합니다.
   `InMemorySaver` 체크포인터를 써서 대화형 모드에서는 같은 `thread_id`로 후속 질문을 이어가거나, 오류로 중단된 실행을 `resume`으로 재개할 수 있습니다.

---

## 기술 스택

| 구분 | 기술 |
|---|---|
| **Agent** | LangGraph (Supervisor + ReAct 서브그래프, Send fan-out), LangChain Core |
| **LLM** | OpenAI `gpt-5-mini` (`OPENAI_MODEL`로 변경 가능) |
| **RAG** | FAISS + `BAAI/bge-m3` (로컬 임베딩), PyMuPDF 파싱, 벡터 + bigram 키워드 하이브리드 검색 |
| **데이터** | Tavily (웹 검색), 문서 풀 PDF, OpenDART API (국내 상장사 공시), yfinance (시가총액) |
| **실행** | Python CLI |

---

## 프로젝트 구조

```
.
├── data/                  # 문서 풀
│   ├── docs/              # 원본 PDF (Physical AI · 로보틱스 보고서)와 metadata.json (서지 정보)
│   ├── vectorstores/      # 문서 풀 FAISS 인덱스 (BAAI--bge-m3)
│   └── corp_codes.json    # DART 기업 코드 캐시 (실행 시 생성)
├── agents/                # Agent 모듈
│   ├── graph.py           # Supervisor 멀티 에이전트 그래프
│   ├── state.py           # State 스키마 (제어 메타 / 페이로드)
│   ├── criteria.py        # 투자 평가 항목 · 스코어카드 계산
│   ├── report.py          # 투자 보고서 형식 검사 (목차 · REFERENCE 표기)
│   ├── tools.py           # 웹 검색 · 문서 풀 · DART · 스코어카드 도구
│   ├── docs.py            # 문서 풀 인덱싱 · 하이브리드 검색 (FAISS)
│   ├── dart.py            # OpenDART API 클라이언트
│   └── trace.py           # 실행 트레이스 (JSONL)
├── prompts/               # 프롬프트 템플릿
│   ├── supervisor.md
│   ├── company_researcher.md
│   ├── tech_researcher.md
│   ├── market_researcher.md
│   ├── evaluator.md
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
TAVILY_API_KEY=tvly-...   # https://tavily.com 에서 발급 (웹 검색용)
OPENDART_API_KEY=...      # https://opendart.fss.or.kr 에서 발급 (국내 상장사 공시용)
OPENAI_MODEL=gpt-5-mini   # 선택
```

### 2. 설치

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`data/docs`와 `data/vectorstores`는 저장소에 포함되지 않습니다. `data/docs`에 PDF와 `metadata.json`을 넣고 인덱스를 만듭니다.

```bash
python -m agents.docs
```

### 3. 실행

```bash
python app.py                                  # 대화형 모드
python app.py "Figure AI 투자 가능성 평가해줘"   # 단일 질문
```

진행 상황이 터미널에 출력되고, 완성된 보고서는 `outputs/report_YYYYMMDD_HHMMSS.md`로, 결정·사유·작업 결과는 `outputs/traces/<run_id>.jsonl`로 저장됩니다.

---

> 본 프로젝트의 보고서는 공개 자료에 기반한 참고용이며 투자 권유가 아닙니다.
