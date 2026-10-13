"""문서 풀 RAG.

data/docs 의 PDF(산업 전략·정책, 연구기관·컨설팅 전망 보고서)로 미리 만들어 둔 FAISS 인덱스를 검색한다.
- 인덱스: data/vectorstores/BAAI--bge-m3 (LangChain FAISS 형식, 정규화된 bge-m3 임베딩)
- 질의도 같은 모델(BAAI/bge-m3)로 임베딩해야 하므로 처음 검색할 때 모델을 로컬에 내려받는다.
"""

import threading

from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings

from . import dart
from .rag import CANDIDATES, KEYWORD_WEIGHT, _keyword_score

STORE_DIR = dart.DATA_DIR / "vectorstores" / "BAAI--bge-m3"
EMBED_MODEL = "BAAI/bge-m3"

# 도구는 여러 스레드에서 동시에 호출된다. 임베딩 모델을 동시에 돌리면 프로세스가 죽으므로(macOS MPS)
# 모델 로드와 벡터 검색을 한 번에 하나씩만 실행한다.
_lock = threading.Lock()
_store: FAISS | None = None


def _load() -> FAISS:
    global _store
    if _store is None:
        if not (STORE_DIR / "index.faiss").exists():
            raise FileNotFoundError(f"문서 풀 인덱스가 없습니다: {STORE_DIR}")
        embeddings = HuggingFaceEmbeddings(model_name=EMBED_MODEL, encode_kwargs={"normalize_embeddings": True})
        # index.pkl 은 직접 만든 인덱스 파일이므로 역직렬화를 허용한다
        _store = FAISS.load_local(str(STORE_DIR), embeddings, allow_dangerous_deserialization=True)
    return _store


def search(query: str, k: int = 5) -> str:
    """하이브리드 검색: 벡터 유사도로 후보를 넉넉히 뽑은 뒤 키워드 일치도를 더해 재정렬."""
    with _lock:
        store = _load()
        candidates = store.similarity_search_with_score(query, k=min(CANDIDATES, store.index.ntotal))
    scored = sorted(
        # 정규화된 벡터의 L2 거리 제곱 d 는 코사인 유사도 1 - d/2 에 해당한다
        ((1 - dist / 2) + KEYWORD_WEIGHT * _keyword_score(query, doc.page_content), i)
        for i, (doc, dist) in enumerate(candidates)
    )[::-1][:k]
    lines = [f"[문서 풀 검색: '{query}']"]
    for rank, (score, i) in enumerate(scored, 1):
        doc = candidates[i][0]
        m = doc.metadata
        lines.append(f"\n--- 결과 {rank} (점수 {score:.2f}) ---\n"
                     f"출처: {m.get('title', '제목 없음')} | {m.get('source', '')} | {m.get('date', '')} | "
                     f"p.{m.get('page', '?')} | {m.get('url', '')}\n{doc.page_content}")
    return "\n".join(lines)
