# 문서 풀 (data/docs)

에이전트가 검색(RAG)하는 자료 모음이다. PDF(`.pdf`)와 수집 메모(`.md`)를 모두 읽는다.

| 폴더 | 용도 | 검색 도구 |
|---|---|---|
| `industry/` | 로보틱스·Physical AI 산업·시장 자료 | `search_industry_docs` (market_researcher) |
| `companies/<DART 회사명>/` | 기업별 IR·제품 자료·기사 | `search_company_docs` (researcher) |

- `.md` 수집 메모는 맨 위 YAML 머리말(`title`, `source`, `url`, `published`, `collected`)이 REFERENCE에 그대로 쓰인다.
- 공개 웹 자료를 원문 수치 그대로 요약해 수집한 것이며, 원문 링크로 검증할 수 있다.
- PDF를 추가하면 다음 실행 때 자동으로 다시 인덱싱된다 (파일 목록·수정시각 해시가 바뀌면 새 인덱스).
